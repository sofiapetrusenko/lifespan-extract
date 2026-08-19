"""Extraction: record assembly, provenance, identity, and every failure path.

Records are checked against `schema/experiment.schema.json` with a validator
built here, independently of the one the extractor uses — a test that trusted
the code's own validator would pass whatever that validator happened to accept.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from extract import extract as extract_module
from extract.errors import (
    ExperimentIdCollisionError,
    ExtractError,
    ModelResponseError,
    RecordValidationError,
)
from extract.extract import (
    _CONFUSABLES,
    _ID_PATTERN,
    _SEPARATOR,
    _SUBSTITUTE,
    _TRANSLITERATIONS,
    ABSTRACT,
    EXTRACTED_FROM,
    EXTRACTION_MODEL,
    FULL_TEXT,
    _ascii,
    _binomial_slug,
    _experiment_id,
    _organism_identity,
    _slug,
    _split,
    extract_record,
    identity_schema,
    outcome_schema,
    schema_document,
)
from extract.schema import (
    CODE_ASSIGNED_CLAIM_KEYS,
    CODE_ASSIGNED_EXPERIMENT_KEYS,
    EXPERIMENT_INDEX,
    IDENTITY_PROPERTIES,
    OUTCOME_PROPERTIES,
    SCHEMA_PATH,
)
from ingest.models import RawPaper
from tests.conftest import (
    QUOTE,
    StubClient,
    claim,
    claims,
    experiment_payload,
    extraction_responses,
    identity_payload,
    model_response,
    outcome_payload,
)

TITLE = "Rapamycin fed late in life extends lifespan"
TEXT = (
    "Rapamycin fed beginning at 600 days of age extended median survival by 14% in "
    "genetically heterogeneous mice."
)

VALIDATOR = Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))

GOLD_DIR = Path(__file__).resolve().parent.parent / "data" / "gold"

# Every gold `experiment_id` the generator does not reproduce, as
# `stem -> ((gold_id, generated_id), ...)`. Fifteen of the gold set's
# twenty-six ids, from two systematic differences:
#
# 1. **Disambiguation.** One paper reporting the same (organism, agent) pair
#    twice is `-male`/`-female` or `-low-dose`/`-high-dose` by hand, and `-2`,
#    `-3` here. The generator has no way to know which axis split the arms.
# 2. **Agent naming.** The labeller normalises what the paper printed
#    (`nordihydroguaiaretic acid (NDGA)` -> `ndga`); `_slug` takes the value as
#    it stands.
#
# This is a pin, not an endorsement: the generator follows the convention the
# schema documents, the gold set follows another, and settling which one wins
# is the human's call — see NOTES.md (2026-08-12). Pinned so that a change on
# either side breaks a test instead of passing quietly, because Phase 3 aligns
# gold against extracted output and would otherwise inherit the gap silently.
KNOWN_ID_DIVERGENCES: dict[str, tuple[tuple[str, str], ...]] = {
    "calubag2025": (
        ("calubag2025-mmusculus-valine-restriction-male",
         "calubag2025-mmusculus-valine-restriction"),
        ("calubag2025-mmusculus-valine-restriction-female",
         "calubag2025-mmusculus-valine-restriction-2"),
    ),
    "harrison2009": (
        ("harrison2009-mmusculus-rapamycin-male", "harrison2009-mmusculus-rapamycin"),
        ("harrison2009-mmusculus-rapamycin-female", "harrison2009-mmusculus-rapamycin-2"),
    ),
    "kenyon1993": (
        ("kenyon1993-celegans-daf-2", "kenyon1993-celegans-daf-2-mutation"),
    ),
    "lakowski1998": (
        ("lakowski1998-celegans-eat-2",
         "lakowski1998-celegans-eat-2-mutation-reference-allele-ad465"),
    ),
    "martinmontalvo2013": (
        ("martinmontalvo2013-mmusculus-metformin-low-dose",
         "martinmontalvo2013-mmusculus-metformin"),
        ("martinmontalvo2013-mmusculus-metformin-high-dose",
         "martinmontalvo2013-mmusculus-metformin-2"),
    ),
    "miller2011": (
        ("miller2011-mmusculus-rapamycin-male", "miller2011-mmusculus-rapamycin"),
        ("miller2011-mmusculus-rapamycin-female", "miller2011-mmusculus-rapamycin-2"),
    ),
    "strong2016": (
        ("strong2016-mmusculus-ndga", "strong2016-mmusculus-nordihydroguaiaretic-acid-ndga"),
        ("strong2016-mmusculus-acarbose-male", "strong2016-mmusculus-acarbose"),
        ("strong2016-mmusculus-acarbose-female", "strong2016-mmusculus-acarbose-2"),
        ("strong2016-mmusculus-metformin-rapamycin",
         "strong2016-mmusculus-metformin-plus-rapamycin"),
        ("strong2016-mmusculus-udca", "strong2016-mmusculus-ursodeoxycholic-acid-udca"),
    ),
}


def paper(**overrides: Any) -> RawPaper:
    fields: dict[str, Any] = {
        "source": "pubmed",
        "source_id": "19587680",
        "pmid": "19587680",
        "doi": "10.1038/nature08221",
        "title": TITLE,
        "abstract": TEXT,
        "year": 2009,
        "first_author": "Harrison",
    }
    fields.update(overrides)
    return RawPaper.build(**fields)


def test_the_provenance_vocabulary_is_the_schema_s():
    """A drift guard: the stamped values are the ones the schema allows."""
    document = json.loads(SCHEMA_PATH.read_text())
    assert list(EXTRACTED_FROM) == document["$defs"]["extracted_from"]["enum"]


def test_a_record_validates_against_the_real_schema():
    client = StubClient(*extraction_responses())
    record = extract_record(paper(), TEXT, client=client)

    VALIDATOR.validate(record)
    assert record["schema_version"] == json.loads(SCHEMA_PATH.read_text())["x-schema-version"]
    assert record["paper"] == {
        "doi": "10.1038/nature08221",
        "title": TITLE,
        "year": 2009,
        "source": "pubmed",
        "pmid": "19587680",
    }


def test_both_requests_use_the_extraction_model_and_their_own_derived_schema():
    """Two calls, one model, and the two halves of the schema — never the same one twice."""
    client = StubClient(*extraction_responses())
    extract_record(paper(), TEXT, client=client)

    identity, outcome = client.requests
    assert identity["model"] == outcome["model"] == EXTRACTION_MODEL
    assert identity["output_config"]["format"]["schema"] == identity_schema()
    assert outcome["output_config"]["format"]["schema"] == outcome_schema()
    assert identity_schema() != outcome_schema()
    for request in (identity, outcome):
        assert TEXT in request["messages"][0]["content"]


def test_the_second_call_is_told_what_the_first_one_found():
    """The join is by index, so the second call has to be shown the numbering.

    Without the echo the model is asked for `experiment_index` values against a
    list it cannot see, and the merge is left to reject whatever it guesses.
    """
    client = StubClient(
        *extraction_responses(
            experiment_payload(organism="C. elegans", agent="spermidine"),
            experiment_payload(agent="rapamycin"),
        )
    )
    extract_record(paper(), TEXT, client=client)

    listing = client.requests[1]["messages"][0]["content"]
    assert "0. " in listing and "1. " in listing
    assert "spermidine" in listing and "rapamycin" in listing
    # Values only: the echo is there to tell the experiments apart, and quoting
    # the first call's quotes back would invite the second call to reuse them.
    assert QUOTE not in listing.split(TEXT, 1)[1]


def test_the_quote_haystack_is_the_paper_prompt_and_not_the_echoed_list(monkeypatch):
    """The two are built from one string so they cannot drift — pinned in both directions.

    A narrower haystack rejects a real quote and throws away the record; that
    half is pinned by the title test below. A *wider* one is the dangerous half
    and nothing was watching it: every verbatim test here supplies a quote that
    is either in the prompt or nowhere near it, so a haystack carrying text the
    model was never shown passes them all, and a quote from that text verifies
    against a paper the model did not read.

    The split gives that half a concrete shape. The second call's user message
    is the paper prompt *plus* the list of what the first call found, and that
    list is model output rather than source text — so a quote lifted from it
    must not verify. The haystack is therefore the first call's message
    exactly, which is the paper prompt, and a strict prefix of the second's.

    None of the three sides of this is a literal: all come out of the run.
    """
    seen: list[str] = []
    verbatim = extract_module.check_quotes_verbatim

    def recording(record: Any, text: str) -> None:
        seen.append(text)
        verbatim(record, text)

    monkeypatch.setattr(extract_module, "check_quotes_verbatim", recording)

    client = StubClient(*extraction_responses())
    extract_record(paper(), TEXT, client=client)

    (haystack,) = seen
    identity, outcome = (request["messages"][0]["content"] for request in client.requests)
    assert haystack == identity
    assert haystack != outcome
    assert outcome.startswith(haystack) and len(outcome) > len(haystack)


def test_a_quote_lifted_from_the_echoed_list_is_a_fabrication():
    """The line the echo adds is not text of the paper, and quoting it must fail.

    This is the concrete failure the haystack choice prevents: the second call
    is shown a summary of the first call's output, so a model that quotes that
    summary produces a `source_quote` which is verbatim in its own prompt and
    appears nowhere in the paper. Widening the haystack to the whole user
    message — the obvious simplification — would verify it.
    """
    identities = identity_payload()
    listed = extract_module._outcome_prompt("Title: t\n\nbody", identities["experiments"])
    echoed = listed.splitlines()[-1]
    assert "organism=" in echoed  # premise: this line is the echo, not the paper

    outcomes = outcome_payload()
    outcomes["experiments"][0]["mechanism"] = claim("mTOR inhibition", echoed)
    client = StubClient(model_response(identities), model_response(outcomes))

    with pytest.raises(RecordValidationError, match="not verbatim"):
        extract_record(paper(), TEXT, client=client)


def test_the_schema_document_is_loaded_once_per_process():
    """Object identity, not equality: `validate_record` caches on `is`.

    Its compiled validator is rebuilt whenever `_validator.schema is not
    document`, so a loader that returned an equal-but-fresh dict would recompile
    the whole schema for every paper in the batch while every assertion about
    the record still passed.
    """
    assert schema_document() is schema_document()


def test_paper_metadata_is_never_taken_from_the_model():
    """A model that volunteers bibliographic data cannot influence the record."""
    volunteered = {**identity_payload(), "paper": {"doi": "10.0000/fake"}}
    client = StubClient(model_response(volunteered), model_response(outcome_payload()))
    record = extract_record(paper(), TEXT, client=client)
    assert record["paper"]["doi"] == "10.1038/nature08221"


def test_extracted_from_is_stamped_from_the_caller_not_the_model():
    client = StubClient(*extraction_responses())
    record = extract_record(paper(), TEXT, client=client, extracted_from=ABSTRACT)

    stamped = {c["extracted_from"] for c in claims(record)}
    assert stamped == {ABSTRACT}
    # Every claim in the schema, not just the top-level ones: 6 on the
    # experiment, 4 under intervention, 5 under lifespan_effect.
    assert len(claims(record)) == 15


def test_full_text_provenance_is_stamped_when_that_is_what_was_read():
    client = StubClient(*extraction_responses())
    record = extract_record(paper(), TEXT, client=client, extracted_from=FULL_TEXT)
    assert {c["extracted_from"] for c in claims(record)} == {FULL_TEXT}


# One plausible volunteered value per key the caller assigns rather than asks
# for. Keyed off the sets in `extract.schema`, so a key added there and not
# guarded fails `test_every_code_assigned_key_is_covered_here` instead of
# going untested.
VOLUNTEERED: dict[str, Any] = {
    "experiment_id": "harrison2009-mmusculus-rapamycin",
    "notes": "Dose converted from ppm to mg/kg.",
    "extracted_from": "full_text",
}


def test_every_code_assigned_key_is_covered_here():
    assert set(VOLUNTEERED) == CODE_ASSIGNED_EXPERIMENT_KEYS | CODE_ASSIGNED_CLAIM_KEYS


@pytest.mark.parametrize("key", sorted(CODE_ASSIGNED_CLAIM_KEYS))
def test_a_model_supplied_provenance_claim_raises(key):
    """The request schema forbids the key; if it appears, the response is wrong."""
    rogue = experiment_payload()
    rogue["organism"][key] = VOLUNTEERED[key]
    client = StubClient(*extraction_responses(rogue))

    with pytest.raises(ModelResponseError, match=key):
        extract_record(paper(), TEXT, client=client)


@pytest.mark.parametrize("key", sorted(CODE_ASSIGNED_EXPERIMENT_KEYS))
@pytest.mark.parametrize("call", ["identity", "outcome"])
def test_a_model_supplied_experiment_key_raises(key, call):
    """The same rule as `extracted_from`, for the keys that sit on an experiment.

    Both are pruned from the request schema, so a response carrying one did not
    follow the schema — and neither of the quiet outcomes is acceptable. A
    volunteered `notes` was persisted into the record: schema-valid, carrying no
    provenance, never scored, in the one field documented as human annotation. A
    volunteered `experiment_id` was overwritten and dropped without a word.

    Once per call, because there are two responses to volunteer it in and the
    guard has to be on both. The outcome half is the one where an unnoticed key
    goes quietest: the merge copies the fields it knows about, so a `notes` the
    model wrote there would be dropped without ever reaching the record or an
    error message.
    """
    identities, outcomes = identity_payload(), outcome_payload()
    {"identity": identities, "outcome": outcomes}[call]["experiments"][0][key] = VOLUNTEERED[key]
    client = StubClient(model_response(identities), model_response(outcomes))

    with pytest.raises(ModelResponseError, match=key):
        extract_record(paper(), TEXT, client=client)


def test_an_unknown_provenance_argument_is_rejected():
    with pytest.raises(ValueError, match="extracted_from"):
        extract_record(paper(), TEXT, client=StubClient(), extracted_from="pdf")


def test_empty_text_is_refused_before_any_call():
    client = StubClient()
    with pytest.raises(ExtractError, match="no text was supplied"):
        extract_record(paper(), "  ", client=client)
    assert client.requests == []


def test_a_multi_organism_paper_yields_one_record_per_organism():
    client = StubClient(
        *extraction_responses(
                experiment_payload(organism="C. elegans", agent="spermidine"),
                experiment_payload(organism="other", species="S. cerevisiae", agent="spermidine"),
                experiment_payload(
                    organism="other", species="D. melanogaster", agent="spermidine"
                ),
            )
    )
    record = extract_record(paper(first_author="Eisenberg", year=2009), TEXT, client=client)

    VALIDATOR.validate(record)
    assert [e["experiment_id"] for e in record["experiments"]] == [
        "eisenberg2009-celegans-spermidine",
        "eisenberg2009-scerevisiae-spermidine",
        "eisenberg2009-dmelanogaster-spermidine",
    ]


def test_two_interventions_in_one_organism_yield_two_records():
    client = StubClient(
        *extraction_responses(
                experiment_payload(agent="rapamycin"),
                experiment_payload(agent="metformin"),
            )
    )
    record = extract_record(paper(), TEXT, client=client)
    assert [e["experiment_id"] for e in record["experiments"]] == [
        "harrison2009-mmusculus-rapamycin",
        "harrison2009-mmusculus-metformin",
    ]


def test_a_repeated_pair_takes_the_schema_s_numeric_suffix_not_the_gold_set_s_wording():
    """Pins the convention `schema/experiment.schema.json` documents: `-2`, `-3`.

    Not the convention `data/gold/` actually uses. Where one paper reports the
    same (organism, agent) pair twice, the human labeller disambiguates
    semantically — `-male`, `-female`, `-low-dose` — and no gold record carries
    a numeric suffix. The generator cannot reproduce that from the payload, and
    which convention wins is the human's call, not this test's:
    `test_generated_ids_match_gold_except_where_pinned` measures the gap, and
    NOTES.md (2026-08-12) records it as an open question for Phase 3.

    The three arms differ by `sex`, which is the axis `harrison2009` splits
    them on and which the id generator does not read — so they are three
    distinguishable experiments taking the numeric tail, rather than three
    copies of one that no assertion here or downstream could tell apart.
    """
    client = StubClient(
        *extraction_responses(
            experiment_payload(sex=claim("male")),
            experiment_payload(sex=claim("female")),
            experiment_payload(sex=claim("mixed")),
        )
    )
    record = extract_record(paper(), TEXT, client=client)

    VALIDATOR.validate(record)
    assert [e["experiment_id"] for e in record["experiments"]] == [
        "harrison2009-mmusculus-rapamycin",
        "harrison2009-mmusculus-rapamycin-2",
        "harrison2009-mmusculus-rapamycin-3",
    ]


def agent_id(agent: str, *, first_author: str = "Strong", year: int = 2016) -> str:
    """Return the generated `experiment_id` for a one-experiment paper on `agent`."""
    client = StubClient(*extraction_responses(experiment_payload(agent=agent)))
    record = extract_record(paper(first_author=first_author, year=year), TEXT, client=client)
    VALIDATOR.validate(record)
    return record["experiments"][0]["experiment_id"]


def test_a_greek_letter_in_an_agent_name_is_transliterated_not_dropped():
    """The two estradiols of `data/gold/strong2016.json` must not become one id.

    Dropping the Greek letter collapsed `17-α-estradiol` and `17-β-estradiol`
    onto `strong2016-mmusculus-17-estradiol`, leaving two different compounds
    distinguishable only by the anonymous `-2` tail — and unable to produce
    `strong2016-mmusculus-17-alpha-estradiol`, the example
    `schema/experiment.schema.json` documents for this very paper.
    """
    alpha = agent_id("17-α-estradiol")
    beta = agent_id("17-β-estradiol")

    assert alpha == "strong2016-mmusculus-17-alpha-estradiol"
    assert beta == "strong2016-mmusculus-17-beta-estradiol"
    assert alpha != beta
    assert "17-estradiol" not in (alpha, beta)


def test_the_unicode_hyphen_spelling_reaches_the_same_id_as_the_ascii_one():
    """U+2010 is the hyphen `strong2016`'s source quotes actually use.

    It is not ASCII, so dropping it took the word boundaries with it and gave
    `17estradiol`. Both spellings name one compound and must slug alike.
    """
    assert agent_id("17‐α‐estradiol") == agent_id("17-α-estradiol")
    assert agent_id("17‐β‐estradiol") == "strong2016-mmusculus-17-beta-estradiol"


def test_greek_letters_that_differ_only_by_the_letter_do_not_collide():
    alpha = agent_id("α-tocopherol", first_author="Morley", year=2002)
    gamma = agent_id("γ-tocopherol", first_author="Morley", year=2002)

    assert alpha == "morley2002-mmusculus-alpha-tocopherol"
    assert gamma == "morley2002-mmusculus-gamma-tocopherol"
    assert alpha != gamma


def test_an_untransliterable_character_becomes_a_separator_not_nothing():
    """An em-dash carries a word boundary even though it has no ASCII reading."""
    assert agent_id("caloric restriction—long term", first_author="Weindruch", year=1986) == (
        "weindruch1986-mmusculus-caloric-restriction-long-term"
    )


@pytest.mark.parametrize(
    ("agent", "slug"),
    [
        ("17-α-estradiol", "17-alpha-estradiol"),
        ("17‐β‐estradiol", "17-beta-estradiol"),
        ("γ-tocopherol", "gamma-tocopherol"),
        ("caloric restriction—long term", "caloric-restriction-long-term"),
        # NFKD folds the micro sign onto a Greek mu, which then transliterates.
        ("10 µM resveratrol", "10-mum-resveratrol"),
    ],
)
def test_non_ascii_agents_still_produce_a_valid_identifier(agent, slug):
    """Every character leaves a mark, and the result is still a schema-legal id.

    The pattern check is the half that matters when a transliteration goes
    wrong in the other direction: a mapping to nothing, or to a leading or
    doubled separator, produces an id the schema rejects.
    """
    generated = agent_id(agent)

    assert generated == f"strong2016-mmusculus-{slug}"
    assert _ID_PATTERN.match(generated)


def test_an_accented_latin_letter_still_folds_to_its_base_letter():
    """NFKD folding must survive the change: `é` is wanted as `e`, not as `-`."""
    assert agent_id("rapamycin", first_author="Peña") == "pena2016-mmusculus-rapamycin"


# --- the transliteration table: no dead entry, and no gap in its stated rule ---
#
# Both directions are tested, because both have failed. A review once deleted
# 39 of the table's 64 entries with the whole suite still green, and the same
# table went four rounds without holding the letters its own comment claimed.
# `test_every_transliteration_entry_is_the_reading_that_character_gets` makes an
# unused entry impossible; the two coverage tests below make a missing one
# impossible for the characters the rule names.


@pytest.mark.parametrize(
    ("character", "reading"), sorted(_TRANSLITERATIONS.items()), ids=lambda value: repr(value)
)
def test_every_transliteration_entry_is_the_reading_that_character_gets(character, reading):
    """Every entry is load-bearing: delete one and this fails for that character.

    Checked through `_slug` as well as `_ascii`, so an entry cannot be correct
    in isolation and lost on the way to an id — the reading has to survive
    collapsing and trimming with a word on either side of it.
    """
    assert _ascii(character) == reading
    assert _SUBSTITUTE not in _ascii(character)
    assert _slug(f"x{character}y") == f"x{reading.lower()}y"


@pytest.mark.parametrize("character", sorted(_TRANSLITERATIONS), ids=lambda value: repr(value))
def test_no_transliteration_entry_is_keyed_on_a_character_that_never_arrives(character):
    """The other way an entry dies: the lookup is never reached with that key.

    `_ascii` keeps ASCII as it stands, runs `_CONFUSABLES` first and NFKD
    second, so a key that is ASCII, or that either of those rewrites, is
    unreachable however right its reading looks. The micro sign is the live
    example — NFKD turns it into a Greek mu before this table is consulted, so
    an entry keyed on it would sit here reading correctly and doing nothing.
    """
    assert not character.isascii()
    assert character not in _CONFUSABLES
    assert unicodedata.normalize("NFKD", character) == character


# The letter categories, as against the modifier letters (Lm) that behave as
# accents: `ŉ` decomposes to a modifier apostrophe plus `n`, and `Ŀ` to `L`
# plus a middle dot, so neither is a letter this table owes a reading to.
LETTER_CATEGORIES = frozenset({"Lu", "Ll", "Lt", "Lo"})

# Latin-1 Supplement and Latin Extended-A: the blocks European author names are
# written in.
LATIN_BLOCKS = range(0x00A0, 0x0180)

GREEK_ALPHABET = "αβγδεζηθικλμνξοπρσςτυφχψω"


def latin_letters_nfkd_cannot_reduce() -> list[str]:
    """Return the letters the table's stated membership rule is about.

    A letter of Latin-1 Supplement or Latin Extended-A that NFKD leaves as
    another letter rather than as an ASCII base plus a combining mark — that is
    exactly the set NFKD cannot handle and the table must, derived here rather
    than listed so that neither this test nor the table can be written to fit
    the other.
    """
    letters = []
    for code_point in LATIN_BLOCKS:
        character = chr(code_point)
        if not character.isalpha():
            continue
        residue = [
            part
            for part in unicodedata.normalize("NFKD", character)
            if not part.isascii() and unicodedata.category(part) in LETTER_CATEGORIES
        ]
        if residue:
            letters.append(character)
    return letters


def test_the_derived_latin_universe_is_the_one_the_reviews_named():
    """Premise of the coverage test: an empty or wrong universe would pass it."""
    letters = latin_letters_nfkd_cannot_reduce()

    assert len(letters) == 24
    # The letters four rounds of review found missing, plus the micro sign,
    # which is in this block and is handled a step earlier by `_CONFUSABLES`.
    assert set("ıħŋĸŧµ") <= set(letters)


@pytest.mark.parametrize(
    "character", latin_letters_nfkd_cannot_reduce(), ids=lambda value: repr(value)
)
def test_every_latin_letter_nfkd_cannot_reduce_has_a_reading(character):
    """No gap in the Latin half of the rule, whatever route the reading takes.

    Asserted against `_ascii`'s output rather than against `_TRANSLITERATIONS`
    membership, so a character handled by `_CONFUSABLES` counts as covered and
    the table is not made to carry an entry it does not need.
    """
    rendering = _ascii(character)

    assert _SUBSTITUTE not in rendering
    assert rendering.isascii() and rendering.isalpha()


@pytest.mark.parametrize(
    "character",
    sorted(set(GREEK_ALPHABET) | set(GREEK_ALPHABET.upper())),
    ids=lambda value: repr(value),
)
def test_every_greek_letter_has_a_reading_in_both_cases(character):
    """The other half of the rule. A Greek letter is a whole compound's name."""
    rendering = _ascii(character)

    assert _SUBSTITUTE not in rendering
    assert rendering.isascii() and rendering.isalpha()


@pytest.mark.parametrize(
    ("name", "slug"),
    [("Yıldız", "yildiz"), ("Işık", "isik"), ("Søren", "soren"), ("Þórir", "thorir")],
)
def test_a_surname_outside_ascii_still_names_the_paper(name, slug):
    """The author half of the same table: `Yıldız` used to slug to `y-ld-z`."""
    assert agent_id("rapamycin", first_author=name) == f"{slug}2016-mmusculus-rapamycin"


@pytest.mark.parametrize(
    ("character", "reading"),
    [("ı", "i"), ("ə", "e"), ("ħ", "h"), ("ŋ", "ng"), ("ĸ", "k"), ("ŧ", "t"), ("ẞ", "ss")],
)
def test_the_base_letters_the_table_used_to_be_missing(character, reading):
    """Each of these fell through to a separator and took its word apart."""
    assert _slug(f"{character}-test") == f"{reading}-test"


# --- confusables: one spelling per character, resolved before anything else ---


def test_both_spellings_of_delta_converge_without_colliding_with_a_bare_nine():
    """The iteration-4 finding, in one assertion.

    U+2206 INCREMENT has no reading, so it became a separator, and the trailing
    strip then deleted it because it was leading — `∆9-…` turned into `9-…`,
    which a compound literally named `9-…` already owns, while U+0394 turned
    into `delta9-…`. One character, two compounds, three spellings, and the two
    that mean the same thing were the two that did not match.
    """
    increment = agent_id("∆9-tetrahydrocannabinol")
    greek = agent_id("Δ9-tetrahydrocannabinol")
    bare = agent_id("9-tetrahydrocannabinol")

    assert increment == greek == "strong2016-mmusculus-delta9-tetrahydrocannabinol"
    assert bare == "strong2016-mmusculus-9-tetrahydrocannabinol"
    assert increment != bare


def test_the_micro_sign_and_greek_mu_are_one_character_by_the_time_the_table_sees_it():
    """NFKD folds one onto the other, so only a pass that runs first can align them.

    They read as `mu`, the reading every other Greek letter in the table gets;
    `_CONFUSABLES` documents why not `micro` and not `u`. What matters here is
    that the two spellings cannot drift apart, whatever the reading is.
    """
    assert agent_id("10 µM resveratrol") == agent_id("10 μM resveratrol")
    assert agent_id("10 µM resveratrol") == "strong2016-mmusculus-10-mum-resveratrol"


def dash_confusables() -> set[str]:
    """Return the characters `_CONFUSABLES` normalises onto an ASCII hyphen."""
    return {dash for dash, reading in _CONFUSABLES.items() if reading == "-"}


@pytest.mark.parametrize("dash", sorted(dash_confusables()), ids=lambda value: repr(value))
def test_every_dash_spelling_reaches_the_hyphen_s_id(dash):
    """A typesetter's dash is an ASCII hyphen doing the same job, wherever it sits.

    Parametrized over `_CONFUSABLES` itself so a dash added there is tested by
    adding it, and one that maps to the wrong thing fails here. The leading
    case is the half that distinguishes the two rules that could produce this:
    U+2212 MINUS SIGN is a maths symbol rather than a dash by Unicode category,
    so nothing but its entry here stops it being read as a character with no
    reading and kept as a boundary mark.
    """
    assert agent_id(f"17{dash}alpha{dash}estradiol") == "strong2016-mmusculus-17-alpha-estradiol"
    assert agent_id(f"{dash}rapamycin{dash}") == "strong2016-mmusculus-rapamycin"


def test_the_dash_family_covers_the_ones_a_paper_actually_uses():
    """Premise of the test above: an empty parametrization would pass it."""
    assert dash_confusables() == set("‐‑‒–—―−")


# --- the boundary rule: a separator standing in for a character is not trimmed ---


@pytest.mark.parametrize("agent", ["Яrapamycin", "rapamycinЯ", "平rapamycin"])
def test_a_character_with_no_reading_at_a_boundary_is_not_trimmed_away(agent):
    """The interior case was tested and the boundary case was the bug.

    A leading character that turns into a separator and is then stripped is the
    deletion behaviour returning by the back door: `Яrapamycin` would come out
    as `rapamycin` and take the other compound's id. It keeps its boundary
    instead, and the id that results is refused by the schema's pattern rather
    than issued — an id nobody can build beats an id two compounds share.
    """
    assert _slug(agent) != "rapamycin"
    assert _slug(agent).replace("-", "") == "rapamycin"

    with pytest.raises(ExtractError, match="identifier pattern"):
        agent_id(agent)


@pytest.mark.parametrize(
    "agent", ["  rapamycin  ", "“rapamycin”", "(rapamycin)", "\trapamycin\n"]
)
def test_punctuation_and_whitespace_around_a_value_are_still_trimmed(agent):
    """The other half of the rule, and the reason `.strip()` cannot express it.

    Nothing here is a claim about what the agent is called, so none of it may
    leave a mark — including the non-ASCII quotation marks, which are trimmed
    for being punctuation and not for being ASCII.
    """
    assert _slug(agent) == "rapamycin"
    assert agent_id(agent) == "strong2016-mmusculus-rapamycin"


def test_a_control_character_in_the_payload_cannot_forge_a_boundary_mark():
    """Model output is untrusted, and `_SUBSTITUTE` is an ASCII control character.

    One arriving in an agent name would otherwise be indistinguishable from the
    mark `_ascii` leaves itself, and would pin a separator to the end of a slug
    that nothing in the name put there. Control characters are word boundaries
    and nothing more.
    """
    assert _slug(f"{_SUBSTITUTE}rapamycin") == "rapamycin"
    assert agent_id(f"{_SUBSTITUTE}rapamycin") == "strong2016-mmusculus-rapamycin"


def test_a_value_with_nothing_but_unreadable_characters_names_nothing():
    """A surname in another script leaves no word to build an id segment from."""
    assert not any(character.isalnum() for character in _slug("Ярема"))


def test_an_agent_with_no_readable_character_is_named_as_the_problem():
    """A slug can be a bare separator, which is truthy and still names nothing.

    An emptiness check passes that through to the pattern check, which reports
    a mangled id string and not the field that produced it. Both refuse the
    record; only one tells the operator which value to go and look at.
    """
    with pytest.raises(ExtractError, match="cannot build an experiment_id"):
        agent_id("Ярема")


# --- the same boundary rule on the id's other half, and the two helpers under it ---
#
# The rule was pinned on `_slug` and nowhere else. `_binomial_slug` is its
# sibling, the id's other call site, and reinstating the full `.strip("-")`
# there left the whole suite green — no test referenced `_binomial_slug`,
# `_organism_identity` or `_split` at all. The live failure it hid: a species
# written `S. cerevisiaeЯ` slugs to `scerevisiae-`, which the pattern refuses,
# and with the strip back it silently becomes `scerevisiae` — the clean
# species' id, one character deleted, the exact defect the `_slug` half of this
# rule exists to prevent.


def species_id(species: str, *, agent: str = "spermidine") -> str:
    """Return the generated id for a one-experiment paper on `species`.

    `organism` is `other` because that is the only case where `species` reaches
    the id at all — see `_organism_identity`.
    """
    client = StubClient(
        *extraction_responses(experiment_payload(organism="other", species=species, agent=agent))
    )
    record = extract_record(paper(first_author="Eisenberg", year=2009), TEXT, client=client)
    return record["experiments"][0]["experiment_id"]


@pytest.mark.parametrize(
    ("marked", "clean", "slug"),
    [
        ("ЯM. musculus", "M. musculus", f"{_SEPARATOR}mmusculus"),
        ("M. musculusЯ", "M. musculus", f"mmusculus{_SEPARATOR}"),
        # U+0405 CYRILLIC CAPITAL LETTER DZE, which renders as a Latin S and
        # is not one. It is the genus letter here, so the marked form loses a
        # whole word rather than a suffix.
        ("Ѕ. cerevisiae", "S. cerevisiae", f"{_SEPARATOR}cerevisiae"),
        ("S. cerevisiaeЯ", "S. cerevisiae", f"scerevisiae{_SEPARATOR}"),
        ("D. melanogaster平", "D. melanogaster", f"dmelanogaster{_SEPARATOR}"),
    ],
)
def test_a_species_with_no_reading_at_a_boundary_keeps_its_mark(marked, clean, slug):
    """The mark survives at the end it stood at, and the clean spelling has none.

    Both halves are asserted because only the pair is evidence: a slug carrying
    an end separator means something is wrong with the value *only* if the same
    name spelled readably carries none.
    """
    assert _binomial_slug(marked) == slug
    assert _binomial_slug(clean) == _binomial_slug(clean).strip(_SEPARATOR)
    assert _binomial_slug(marked) != _binomial_slug(clean)


@pytest.mark.parametrize(
    ("marked", "clean"),
    [
        ("ЯM. musculus", "M. musculus"),
        ("M. musculusЯ", "M. musculus"),
        ("Ѕ. cerevisiae", "S. cerevisiae"),
        ("S. cerevisiaeЯ", "S. cerevisiae"),
        ("D. melanogaster平", "D. melanogaster"),
    ],
)
def test_a_marked_species_is_refused_rather_than_given_the_clean_spellings_id(marked, clean):
    """An id nobody can build beats an id two species share.

    Trimming the mark would hand `S. cerevisiaeЯ` the id belonging to
    `S. cerevisiae`, and a multi-organism paper reporting both would then have
    one id covering two species — which Phase 3 scores as one record.
    """
    assert _ID_PATTERN.match(species_id(clean))

    with pytest.raises(ExtractError, match="identifier pattern"):
        species_id(marked)


@pytest.mark.parametrize(
    ("organism", "species", "expected"),
    [
        ("M. musculus", None, ("mmusculus", "M. musculus")),
        ("C. elegans", None, ("celegans", "C. elegans")),
        # `other` is one word for every organism outside the MVP enum, so
        # without `species` a multi-organism paper's records share an id.
        ("other", "S. cerevisiae", ("scerevisiae", "S. cerevisiae")),
        ("other", "D. melanogaster", ("dmelanogaster", "D. melanogaster")),
        # Nothing to read a species from: `other` is the honest answer, and the
        # source value is the organism rather than an empty string.
        ("other", None, ("other", "other")),
        # An enum member wins outright; a species beside it is never consulted.
        ("M. musculus", "S. cerevisiae", ("mmusculus", "M. musculus")),
    ],
)
def test_the_organism_component_of_an_id_and_the_value_it_was_read_from(
    organism, species, expected
):
    """The slug and its source, which `_experiment_id` needs separately.

    It compares the *values* two ids were built from, not their slugs: two
    slugs being equal is the collision, so they cannot also be the evidence
    about whether the collision matters.
    """
    assert _organism_identity(experiment_payload(organism=organism, species=species)) == expected


@pytest.mark.parametrize(
    ("species", "slug"),
    [
        ("ЯM. musculus", f"{_SEPARATOR}mmusculus"),
        ("M. musculusЯ", f"mmusculus{_SEPARATOR}"),
    ],
)
def test_the_organism_component_carries_a_species_boundary_mark_through(species, slug):
    """`_organism_identity` hands the species to `_binomial_slug` and trims nothing.

    The source value comes back as written rather than as the slug, so a
    collision check downstream can still tell two species apart after their
    slugs have merged.
    """
    assert _organism_identity(
        experiment_payload(organism="other", species=species)
    ) == (slug, species)


@pytest.mark.parametrize(
    ("rendering", "expected"),
    [
        # A mark at an end is kept: a character stood there, and trimming it is
        # the deletion this module exists to prevent.
        (f"{_SUBSTITUTE}mus musculus", (_SEPARATOR, ["mus", "musculus"], "")),
        (f"mus musculus{_SUBSTITUTE}", ("", ["mus", "musculus"], _SEPARATOR)),
        (
            f"{_SUBSTITUTE}mus musculus{_SUBSTITUTE}",
            (_SEPARATOR, ["mus", "musculus"], _SEPARATOR),
        ),
        # Punctuation and whitespace are not kept: they surround a value rather
        # than being part of it, which is the distinction `.strip()` cannot make.
        ("  mus musculus  ", ("", ["mus", "musculus"], "")),
        ("(mus-musculus)", ("", ["mus", "musculus"], "")),
        # A mark between words is already a boundary and adds no end separator.
        (f"mus{_SUBSTITUTE}musculus", ("", ["mus", "musculus"], "")),
        # No word to hang a boundary off. The mark still has to survive, because
        # the caller distinguishes "named nothing" from "named nothing readable".
        (_SUBSTITUTE, ("", [], _SEPARATOR)),
        ("", ("", [], "")),
        ("   ", ("", [], "")),
    ],
)
def test_split_keeps_an_end_separator_exactly_when_a_character_stood_there(rendering, expected):
    """The rule both slug functions are built on, tested where it is written."""
    assert _split(rendering) == expected


# --- the collision guard: a numeric tail never separates two different agents ---


def test_two_different_agents_that_slug_alike_are_refused_not_numbered():
    """`NAD+` and `NAD` are two molecules and one slug: the `+` is trailing ASCII.

    Trimming it is correct — punctuation around a value carries no claim — and
    it is exactly the case where an imperfect slug has to fail loudly. Without
    this guard the second record takes `…-nad-2`, and a numeric tail is the only
    thing left saying they are not two arms of one experiment.
    """
    client = StubClient(
        *extraction_responses(experiment_payload(agent="NAD+"), experiment_payload(agent="NAD"))
    )

    with pytest.raises(ExperimentIdCollisionError) as raised:
        extract_record(paper(), TEXT, client=client)

    message = str(raised.value)
    assert "'NAD+'" in message and "'NAD'" in message
    assert "harrison2009-mmusculus-nad" in message


def test_two_species_that_slug_alike_are_refused_too():
    """The organism half of the same pair: the id is built from both halves.

    `S. cerevisiae` and `S cerevisiae` are one species written two ways, and
    with one agent they take one id. The guard keys on the values rather than
    on the agent alone, because a merge on either half is the same merge.
    """
    client = StubClient(
        *extraction_responses(
                experiment_payload(organism="other", species="S. cerevisiae", agent="spermidine"),
                experiment_payload(organism="other", species="S cerevisiae", agent="spermidine"),
            )
    )

    with pytest.raises(ExperimentIdCollisionError, match="scerevisiae-spermidine"):
        extract_record(paper(first_author="Eisenberg", year=2009), TEXT, client=client)


def test_the_guard_leaves_the_repeated_pair_alone():
    """The numeric tail is legitimate for one intervention reported twice.

    Male and female rapamycin arms in `data/gold/harrison2009.json` are the same
    (organism, agent) values twice over, and the guard must not read that as a
    collision — that is what separates it from a blanket ban on duplicate ids.
    A third arm keeps counting. The arms are spelled out as the gold record
    spells them, by `sex`, which the id generator does not read.
    """
    client = StubClient(
        *extraction_responses(
            experiment_payload(sex=claim("male")),
            experiment_payload(sex=claim("female")),
            experiment_payload(sex=claim("mixed")),
        )
    )
    record = extract_record(paper(), TEXT, client=client)

    assert [e["experiment_id"] for e in record["experiments"]] == [
        "harrison2009-mmusculus-rapamycin",
        "harrison2009-mmusculus-rapamycin-2",
        "harrison2009-mmusculus-rapamycin-3",
    ]


def test_an_agent_named_like_a_numeric_tail_cannot_take_that_tail():
    """The tail is not a free namespace: `rapamycin-2` is somebody's agent name.

    Two rapamycin arms and a compound called `rapamycin-2` in one paper compete
    for `…-rapamycin-2`. Whichever gets there first, the other must not be given
    an id a reader would attribute to it. The two rapamycin arms are the male
    and female arms of one intervention, which is the shape a repeated pair
    actually has; `sex` is no part of the id, so the competition is unchanged.
    """
    client = StubClient(
        *extraction_responses(
                experiment_payload(agent="rapamycin", sex=claim("male")),
                experiment_payload(agent="rapamycin-2"),
                experiment_payload(agent="rapamycin", sex=claim("female")),
            )
    )

    with pytest.raises(ExperimentIdCollisionError, match="rapamycin-2"):
        extract_record(paper(), TEXT, client=client)


# --- the author segment ---


def test_an_author_whose_name_has_no_ascii_reading_is_refused_not_dropped():
    """The year kept the stem truthy, so `not all(parts)` never saw this.

    A Cyrillic surname slugged to nothing and the id came out as
    `2016-mmusculus-rapamycin`: schema-legal, pattern-matching, and shared with
    every other 2016 paper on rapamycin. The intent stated four lines above the
    guard was to skip the paper instead.
    """
    with pytest.raises(ExtractError, match="no ASCII reading"):
        agent_id("rapamycin", first_author="Ярема")


def test_the_author_guard_is_not_the_missing_author_guard():
    """Two different failures, two different messages, both loud."""
    client = StubClient(*extraction_responses())
    with pytest.raises(ExtractError, match="experiment_id convention"):
        extract_record(paper(first_author=None), TEXT, client=client)


def generated_ids(document: dict[str, Any]) -> list[tuple[str, str]]:
    """Return `(gold_id, generated_id)` for every experiment in a gold record.

    The author-year stem is read off the gold id rather than regenerated: gold
    files carry no `first_author`, and taking it from the id keeps the
    comparison on the two segments that actually differ — the organism/agent
    slug and the disambiguation tail.
    """
    year = document["paper"]["year"]
    allocated: dict[str, tuple[str, str]] = {}
    pairs: list[tuple[str, str]] = []
    for experiment in document["experiments"]:
        gold_id = experiment["experiment_id"]
        stem = gold_id.split("-")[0]
        stub = RawPaper.build(
            source="pubmed",
            source_id=stem,
            title=document["paper"]["title"],
            year=year,
            first_author=stem[: -len(str(year))],
        )
        generated = _experiment_id(stub, experiment, allocated)
        pairs.append((gold_id, generated))
    return pairs


@pytest.mark.parametrize("path", sorted(GOLD_DIR.glob("*.json")), ids=lambda p: p.stem)
def test_generated_ids_match_gold_except_where_pinned(path):
    """Measure the generator against the ids Phase 3 will have to align on.

    The schema calls `experiment_id` a stable identity "for eval alignment" and
    says Phase 2 generates it from the same convention the gold set was labelled
    by. It does not, for 15 of 26 records — see `KNOWN_ID_DIVERGENCES` for the
    two reasons and NOTES.md for the open question. This test asserts the
    divergence set exactly, in both directions: a new mismatch fails, and so
    does one that quietly goes away.
    """
    document = json.loads(path.read_text())
    divergent = tuple(
        (gold_id, generated)
        for gold_id, generated in generated_ids(document)
        if gold_id != generated
    )
    assert divergent == KNOWN_ID_DIVERGENCES.get(path.stem, ())


def test_not_reported_survives_extraction_unembellished():
    """The honesty case: absent data stays absent and carries no invented quote.

    Every absence here is stated by this test rather than inherited from the
    fixture, `mechanism` included: `experiment_payload` reports a mechanism by
    default, because an outcome half that is constant across experiments is
    what let the two-call join go untested. An absence a test asserts should be
    one it asked for.
    """
    sparse = experiment_payload()
    sparse["mechanism"] = claim(None)
    sparse["lifespan_effect"]["median_change_pct"] = claim(None)
    sparse["lifespan_effect"]["p_value"] = claim(None)
    client = StubClient(*extraction_responses(sparse))

    record = extract_record(paper(), TEXT, client=client)
    VALIDATOR.validate(record)

    experiment = record["experiments"][0]
    assert experiment["sex"]["value"] == "not_reported"
    assert experiment["sex"]["source_quote"] is None
    assert experiment["strain"]["value"] == "not_reported"
    assert experiment["mechanism"]["value"] is None
    assert experiment["lifespan_effect"]["median_change_pct"]["value"] is None
    assert experiment["lifespan_effect"]["p_value"]["value"] is None
    assert all(c["source_quote"] is None for c in claims(experiment) if c["value"] is None)


def test_a_quote_attached_to_an_absent_value_raises():
    dishonest = experiment_payload()
    dishonest["sex"] = {
        "value": "not_reported",
        "source_quote": "Male and female mice were used.",
        "confidence": "high",
    }
    client = StubClient(*extraction_responses(dishonest))

    with pytest.raises(RecordValidationError, match="nothing to quote"):
        extract_record(paper(), TEXT, client=client)


def test_a_value_without_a_quote_raises():
    unsupported = experiment_payload()
    unsupported["mechanism"] = {
        "value": "mTOR inhibition",
        "source_quote": None,
        "confidence": "low",
    }
    client = StubClient(*extraction_responses(unsupported))

    with pytest.raises(RecordValidationError, match="no source_quote"):
        extract_record(paper(), TEXT, client=client)


def test_the_stub_quote_is_verbatim_in_the_stub_text():
    """The premise of every other test here: the stubs are honest by default.

    If this drifts, the fabrication tests below stop testing fabrication and
    start testing a broken fixture.
    """
    assert QUOTE in TEXT


def test_a_fabricated_quote_is_rejected():
    """Schema-valid and provenance-consistent, and still a fabricated citation."""
    invented = experiment_payload()
    invented["strain"] = claim(
        "UM-HET3", "Naked mole rats were housed in colonies at 30 degrees."
    )
    client = StubClient(*extraction_responses(invented))

    with pytest.raises(RecordValidationError) as raised:
        extract_record(paper(), TEXT, client=client)

    message = str(raised.value)
    assert "experiments[0].strain" in message
    assert "not verbatim" in message
    assert "Naked mole rats" in message


def test_a_fabricated_quote_under_a_number_is_rejected_too():
    """The traversal reaches nested claims, where the invented figures live."""
    invented = experiment_payload()
    invented["lifespan_effect"]["median_change_pct"] = claim(
        99.0, "Lifespan doubled in every cohort we examined."
    )
    client = StubClient(*extraction_responses(invented))

    with pytest.raises(RecordValidationError, match="median_change_pct"):
        extract_record(paper(), TEXT, client=client)


def test_a_quote_wrapped_across_lines_is_accepted():
    """A structured abstract wraps where the paper does not; that is not a difference.

    Whitespace is the only thing collapsed, matching scripts/check_gold.py —
    every other character is compared as it stands.
    """
    wrapped = experiment_payload()
    wrapped["strain"] = claim("UM-HET3", QUOTE.replace(" ", "\n   ", 1))
    client = StubClient(*extraction_responses(wrapped))

    record = extract_record(paper(), TEXT, client=client)
    VALIDATOR.validate(record)


def test_a_quote_taken_from_the_title_is_accepted():
    """The title is shown to the model, so it is text the paper can be quoted from.

    An abstract frequently does not restate the organism or the agent, and then
    the title is the only sentence carrying it — exactly the case a model quotes
    the title for. Verifying against the abstract alone called that a fabricated
    citation and threw the whole record away.
    """
    assert TITLE not in TEXT  # premise: this quote can only have come from the title
    from_title = experiment_payload()
    from_title["intervention"]["agent"] = claim("rapamycin", TITLE)
    client = StubClient(*extraction_responses(from_title))

    record = extract_record(paper(), TEXT, client=client)

    VALIDATOR.validate(record)
    assert record["experiments"][0]["intervention"]["agent"]["source_quote"] == TITLE


def test_a_quote_from_neither_the_title_nor_the_text_is_still_a_fabrication():
    """Widening the haystack to the prompt must not widen it to anything else."""
    invented = experiment_payload()
    invented["intervention"]["agent"] = claim("rapamycin", "Rapamycin fed early in life")
    client = StubClient(*extraction_responses(invented))

    with pytest.raises(RecordValidationError, match="not verbatim"):
        extract_record(paper(), TEXT, client=client)


def test_a_quote_differing_by_one_character_is_not_whitespace_forgiven():
    """The collapse must not become a general fuzzy match: 14% is not 41%."""
    altered = experiment_payload()
    altered["strain"] = claim("UM-HET3", QUOTE.replace("14%", "41%"))
    client = StubClient(*extraction_responses(altered))

    with pytest.raises(RecordValidationError, match="not verbatim"):
        extract_record(paper(), TEXT, client=client)


def test_a_value_the_api_subset_cannot_constrain_is_still_validated():
    """p_value's pattern survives only in the real schema; check it is enforced."""
    malformed = experiment_payload()
    malformed["lifespan_effect"]["p_value"] = claim("not significant")
    client = StubClient(*extraction_responses(malformed))

    with pytest.raises(RecordValidationError, match="p_value"):
        extract_record(paper(), TEXT, client=client)


def test_an_out_of_vocabulary_value_is_rejected():
    invented = experiment_payload(direction="slight_increase")
    client = StubClient(*extraction_responses(invented))

    with pytest.raises(RecordValidationError, match="direction"):
        extract_record(paper(), TEXT, client=client)


def test_a_fenced_payload_is_repaired_without_a_retry():
    body = "```json\n" + json.dumps(identity_payload()) + "\n```"
    client = StubClient(model_response(body), model_response(outcome_payload()))

    record = extract_record(paper(), TEXT, client=client)
    VALIDATOR.validate(record)
    # Two: the repair spent no retry, so these are the two calls extraction makes.
    assert len(client.requests) == 2


def test_an_unparseable_payload_is_retried_once_then_succeeds():
    client = StubClient(model_response("I could not find any experiments."), *extraction_responses())
    record = extract_record(paper(), TEXT, client=client)

    VALIDATOR.validate(record)
    # The retried first call, then the second call.
    assert len(client.requests) == 3


def test_two_unparseable_payloads_raise_and_produce_no_record():
    client = StubClient(model_response("first mess"), model_response("second mess"))
    with pytest.raises(ModelResponseError, match="2 attempt"):
        extract_record(paper(), TEXT, client=client)


def test_an_empty_experiments_array_is_reported_not_papered_over():
    client = StubClient(model_response({"experiments": []}))
    with pytest.raises(ExtractError, match="returned no experiments"):
        extract_record(paper(), TEXT, client=client)


def test_a_payload_without_experiments_raises():
    client = StubClient(model_response({"records": []}))
    with pytest.raises(ModelResponseError, match="experiments"):
        extract_record(paper(), TEXT, client=client)


# --- the join between the two calls ---
#
# The second call returns one outcome per experiment the first call identified,
# each naming its experiment by index. Everything the merge can be handed that
# is not a one-to-one correspondence has a test here, because every one of them
# has a silent reading: a dropped outcome is a record with no result, a
# duplicated index is two experiments sharing one result, and an index naming
# no experiment is a result thrown away. None of them is visible in the
# assembled record — it validates either way — so the merge is the only place
# they can be caught.


def three_experiments() -> list[dict[str, Any]]:
    """Three distinguishable experiments, so an index can be wrong in every way."""
    return [
        experiment_payload(agent="rapamycin"),
        experiment_payload(agent="metformin"),
        experiment_payload(agent="acarbose"),
    ]


def joined(*, outcomes: list[dict[str, Any]]) -> StubClient:
    """A client answering with three experiments and the given outcome entries."""
    experiments = three_experiments()
    return StubClient(
        model_response(identity_payload(*experiments)),
        model_response({"experiments": outcomes}),
    )


def outcome_entries() -> list[dict[str, Any]]:
    """The three well-formed outcomes for `three_experiments`."""
    return outcome_payload(*three_experiments())["experiments"]


def outcome_values(experiment: dict[str, Any]) -> tuple[Any, ...]:
    """Return the outcome half of an experiment as the values a record carries.

    Enough of it to say *which* experiment's outcome this is — which is the
    only question the join tests below ask.
    """
    return (
        experiment["mechanism"]["value"],
        experiment["lifespan_effect"]["direction"]["value"],
        experiment["lifespan_effect"]["median_change_pct"]["value"],
    )


def test_the_three_experiments_have_three_distinguishable_outcomes():
    """Premise of both join tests: alike outcomes would make either of them vacuous.

    This is the fixture condition that was missing. `experiment_payload`'s
    outcome half used to be byte-identical for every experiment any test built,
    so an outcome joined to the wrong experiment produced a record identical to
    the right one and no assertion anywhere could have caught it.
    """
    values = [outcome_values(experiment) for experiment in three_experiments()]

    assert len(set(values)) == 3
    # And on `mechanism` alone, which is the half of the derivation that is
    # injective in the experiment's identity rather than spread across an enum.
    assert len({mechanism for mechanism, _, _ in values}) == 3


# --- the fixture's own premises: which experiments it can tell apart ---
#
# `three_experiments()` varies `agent`, and the tests above rest on the three
# outcomes it produces being distinct. That held for `agent` under the old
# derivation too, which is why the hole below stayed dormant: the derivation
# read three arguments, so nine of the eleven experiments a test could build
# collapsed onto one outcome half — including the male/female and low/high-dose
# pairs the gold set uses to separate experiments within a paper. The three
# tests here pin the derivation itself rather than one fixture's use of it.


def dosed(dose: str) -> dict[str, Any]:
    """The default intervention at `dose`, the axis `martinmontalvo2013` splits on."""
    intervention = experiment_payload()["intervention"]
    intervention["dose"] = claim(dose)
    return intervention


@pytest.mark.parametrize("name", sorted(IDENTITY_PROPERTIES))
def test_varying_any_identity_property_alone_changes_the_outcome_half(name):
    """Every axis reaches the derivation, and the list of axes is the schema's.

    Parametrized over `IDENTITY_PROPERTIES` rather than over the derivation's
    own reading of it, so a property the derivation stops reading fails here
    instead of quietly producing two experiments a join test cannot tell apart.

    A property a later schema version *adds* is not caught here — measured, not
    assumed: this test supplies the property itself as an override, so it
    varies and the assertion holds. What the addition does now is fail in the
    tests that actually depend on the new property, with the rest of the suite
    running; it used to abort collection outright, because the derivation
    subscripted the payload and a property no fixture built raised `KeyError`
    before a single test ran.
    """
    varied = experiment_payload(**{name: claim(f"varied {name}")})
    base = experiment_payload()

    assert outcome_values(varied) != outcome_values(base)
    # On `mechanism` alone as well, which is the injective half of the
    # derivation. `direction` and `median_change_pct` come off a CRC and can
    # coincide for two different experiments without anything being wrong, so
    # a difference in the tuple is not on its own evidence of injectivity.
    assert varied["mechanism"]["value"] != base["mechanism"]["value"]


@pytest.mark.parametrize(
    ("axis", "left", "right"),
    [
        ("sex", {"sex": claim("male")}, {"sex": claim("female")}),
        ("dose", {"intervention": dosed("4 ppm")}, {"intervention": dosed("100 ppm")}),
        (
            "organism or species",
            {"organism": "M. musculus"},
            {"organism": "other", "species": "M. musculus"},
        ),
    ],
)
def test_the_axes_the_gold_set_splits_experiments_on_are_distinguishable(axis, left, right):
    """The three the old derivation collapsed, named for what they cost.

    `harrison2009` and `miller2011` split rapamycin by sex; `martinmontalvo2013`
    splits metformin by dose; `eisenberg2009` reaches its species through
    `organism: "other"`, so `organism="M. musculus"` and `organism="other"` with
    that species are two spellings the derivation must not merge. A future join
    test written on any of these — the natural axes to reach for, being the
    project's own — landed in the collision set and was vacuous.
    """
    one, other = experiment_payload(**left), experiment_payload(**right)

    assert outcome_values(one) != outcome_values(other), axis
    assert one["mechanism"]["value"] != other["mechanism"]["value"], axis


def test_a_fixture_whose_experiments_share_an_outcome_half_is_refused():
    """The guard that makes a vacuous join fixture unwritable rather than unwritten.

    Two experiments with equal outcome halves are indistinguishable to every
    assertion downstream, so `outcome_payload` refuses them at the point the
    fixture is built. It fires on any multi-experiment fixture, which is the
    difference between an instrument and a note in a docstring.
    """
    with pytest.raises(AssertionError, match="share an outcome half"):
        outcome_payload(experiment_payload(), experiment_payload())

    # The ordinary single-experiment fixture is untouched: there is nothing to
    # tell apart, and most tests in this suite build exactly one experiment.
    assert len(outcome_payload(experiment_payload())["experiments"]) == 1


def test_the_well_formed_join_is_the_premise_of_the_failure_tests_below():
    """Three experiments, three outcomes, one each: this is what must succeed.

    Without it the failure tests below could all be passing for a reason that
    has nothing to do with the fault they name. The name carries no count of
    them: it said five while there were eight, and a number in a test's name
    goes stale on the next test added below it.
    """
    record = extract_record(paper(), TEXT, client=joined(outcomes=outcome_entries()))

    VALIDATOR.validate(record)
    assert [e["intervention"]["agent"]["value"] for e in record["experiments"]] == [
        "rapamycin",
        "metformin",
        "acarbose",
    ]
    # Each experiment carries its *own* outcome, not merely some outcome. A
    # merge that reversed or rotated the correspondence would satisfy the
    # weaker check, and satisfy the schema and both provenance invariants too.
    assert [outcome_values(e) for e in record["experiments"]] == [
        outcome_values(e) for e in three_experiments()
    ]
    # And nothing of the transport survives into the record.
    assert not any(EXPERIMENT_INDEX in e for e in record["experiments"])


@pytest.mark.parametrize("order", [(2, 0, 1), (2, 1, 0), (1, 2, 0)])
def test_outcomes_returned_out_of_order_still_land_on_their_own_experiments(order):
    """The join is `experiment_index`, and this is the only test that can see it.

    A reordered response is a *legal* response: nothing asks the model to return
    its outcomes in list order, only that each one names the experiment it
    belongs to, and `OUTCOME_SYSTEM_PROMPT` promises the code will honour that.
    It is also the one shape where a join by index and a join by position
    disagree — in list order the two are the same function.

    Under a positional join every outcome here attaches to the wrong
    experiment, and nothing downstream notices: the record validates, satisfies
    `check_provenance`, and passes `check_quotes_verbatim`, because each quote
    is a real sentence of this same paper wherever it lands. Rapamycin's result
    filed under metformin, with a genuine quote behind it. The three orders are
    a rotation each way and a full reversal, so a merge that shifts or reverses
    the correspondence fails here as loudly as one that ignores the index.
    """
    entries = outcome_entries()
    shuffled = [entries[position] for position in order]

    record = extract_record(paper(), TEXT, client=joined(outcomes=shuffled))

    VALIDATOR.validate(record)
    assert [outcome_values(e) for e in record["experiments"]] == [
        outcome_values(e) for e in three_experiments()
    ]


def test_a_dropped_outcome_raises_rather_than_leaving_an_experiment_resultless():
    """Two outcomes for three experiments. The third would have no result at all."""
    dropped = outcome_entries()[:-1]

    with pytest.raises(ModelResponseError, match=r"\[2\] have no outcome"):
        extract_record(paper(), TEXT, client=joined(outcomes=dropped))


def test_a_duplicated_index_raises_rather_than_merging_two_results_into_one():
    """Two outcomes claiming experiment 0, and experiment 2 silently left bare."""
    duplicated = outcome_entries()
    duplicated[1][EXPERIMENT_INDEX] = 0

    with pytest.raises(ModelResponseError, match=r"\[0\] appear more than once"):
        extract_record(paper(), TEXT, client=joined(outcomes=duplicated))


def test_more_outcomes_than_experiments_raises():
    """A fourth outcome is a result for an experiment the first call did not find.

    Whether it duplicates an index or names a fourth experiment, it is a result
    with nowhere to go, and dropping it would lose a finding the model reported.
    """
    extra = outcome_entries()
    extra.append({**outcome_entries()[0], EXPERIMENT_INDEX: 0})

    with pytest.raises(ModelResponseError, match="do not correspond one-to-one"):
        extract_record(paper(), TEXT, client=joined(outcomes=extra))


@pytest.mark.parametrize("index", [3, 7, -1])
def test_an_index_naming_no_experiment_raises(index):
    """Including -1, which Python would happily read as the last experiment."""
    out_of_range = outcome_entries()
    out_of_range[2][EXPERIMENT_INDEX] = index

    with pytest.raises(ModelResponseError, match=rf"\[{index}\] name no experiment"):
        extract_record(paper(), TEXT, client=joined(outcomes=out_of_range))


@pytest.mark.parametrize("name", sorted(OUTCOME_PROPERTIES))
def test_an_outcome_missing_a_claim_raises(name):
    """The request schema requires every field of it; most are optional in the record.

    So an omission validates perfectly well and lands as an experiment with no
    `mechanism` key at all — which is not the same claim as `mechanism: null`,
    and is indistinguishable downstream from a paper that reports no mechanism.
    """
    incomplete = outcome_entries()
    del incomplete[1][name]

    with pytest.raises(ModelResponseError, match=f"omitted \\['{name}'\\]"):
        extract_record(paper(), TEXT, client=joined(outcomes=incomplete))


@pytest.mark.parametrize("name", sorted(IDENTITY_PROPERTIES))
def test_an_identified_experiment_missing_a_claim_raises(name):
    """The same rule on the first call, where `sex` is the one that matters.

    `sex: not_reported` is a claim the schema has a word for and PLAN.md names
    as a field to watch; a `sex` key that never arrived is not that claim, and
    nothing later can tell them apart.
    """
    experiments = three_experiments()
    identities = identity_payload(*experiments)
    del identities["experiments"][0][name]
    client = StubClient(model_response(identities), model_response({"experiments": []}))

    with pytest.raises(ModelResponseError, match=f"omitted \\['{name}'\\]"):
        extract_record(paper(), TEXT, client=client)


@pytest.mark.parametrize("index", [None, "0", 1.0, True, [0]])
def test_an_index_that_is_not_an_integer_raises(index):
    """`True` is the pointed one: in Python it is an `int`, and it is index 1.

    A JSON `true` would attach a set of results to the second experiment with
    every check downstream satisfied.
    """
    malformed = outcome_entries()
    malformed[0][EXPERIMENT_INDEX] = index

    with pytest.raises(ModelResponseError, match="not an integer"):
        extract_record(paper(), TEXT, client=joined(outcomes=malformed))


def test_an_outcome_with_no_index_at_all_raises():
    orphan = outcome_entries()
    del orphan[0][EXPERIMENT_INDEX]

    with pytest.raises(ModelResponseError, match="not an integer"):
        extract_record(paper(), TEXT, client=joined(outcomes=orphan))


def test_an_unknown_key_on_an_outcome_raises_rather_than_being_dropped():
    """The merge copies the fields it knows; anything else would vanish silently.

    On an identified experiment an unexpected key survives into the record and
    `validate_record` refuses it, because every object in the real schema is
    closed. An outcome has no such backstop — it is read for two fields and an
    index — so it is checked here.
    """
    chatty = outcome_entries()
    chatty[0]["lifespan_effect_summary"] = "increase"

    with pytest.raises(ModelResponseError, match="lifespan_effect_summary"):
        extract_record(paper(), TEXT, client=joined(outcomes=chatty))


@pytest.mark.parametrize("name", sorted(OUTCOME_PROPERTIES))
def test_an_outcome_property_volunteered_on_the_identity_half_raises(name):
    """The first call is not asked for these, and answering with one is refused.

    `mechanism` and `lifespan_effect` are real `experiment` properties, so the
    two backstops that catch every other unasked-for key both miss them: they
    are not code-assigned, so nothing prunes them, and they validate perfectly
    well against the real schema, so `validate_record` would take them. Before
    this guard they reached `_merge_outcomes`, which reported them as an
    overlap between `IDENTITY_PROPERTIES` and `OUTCOME_PROPERTIES` — sending
    the operator to inspect two constants that are correct, in a bare
    `ExtractError` carrying no excerpt of the response that actually caused it.
    """
    experiments = three_experiments()
    identities = identity_payload(*experiments)
    identities["experiments"][0][name] = experiments[0][name]
    client = StubClient(model_response(identities))

    with pytest.raises(ModelResponseError) as caught:
        extract_record(paper(), TEXT, client=client)

    message = str(caught.value)
    assert f"model supplied ['{name}']" in message
    assert "on an identified experiment" in message
    assert "payload excerpt" in message
    # It names the model's answer, not the partition — the constants are fine.
    assert "OUTCOME_PROPERTIES" not in message
    # And it costs one call: the second is never made for a response already
    # known to be wrong.
    assert len(client.requests) == 1


def test_an_invented_key_on_the_identity_half_raises_before_the_second_call():
    """The other shape: a key that is no property of the schema at all.

    This one would have survived into the assembled record and been refused by
    `validate_record` — after the second call had been paid for, and reported
    as a schema failure rather than as a model that answered off-schema.
    """
    identities = identity_payload()
    identities["experiments"][0]["confidence_overall"] = "high"
    client = StubClient(model_response(identities))

    with pytest.raises(ModelResponseError, match=r"model supplied \['confidence_overall'\]"):
        extract_record(paper(), TEXT, client=client)
    assert len(client.requests) == 1


def test_a_failed_second_call_produces_no_record_at_all():
    """The no-partial-record guarantee, at the seam the split introduced.

    The first call succeeded and was paid for; the second did not parse. There
    is no early return in `extract_record` and nothing writes anything, so what
    the caller gets is the exception and no record — the paper stays pending
    and the next run attempts it whole.
    """
    client = StubClient(
        model_response(identity_payload()),
        model_response("the paper does not report lifespan"),
        model_response("still not JSON"),  # ... and its one retry
    )

    with pytest.raises(ModelResponseError, match="2 attempt"):
        extract_record(paper(), TEXT, client=client)
    assert len(client.requests) == 3


def test_the_second_call_is_never_made_when_the_first_one_fails():
    """Nothing is spent reporting outcomes for experiments that were never identified."""
    client = StubClient(model_response({"experiments": []}))

    with pytest.raises(ExtractError, match="returned no experiments"):
        extract_record(paper(), TEXT, client=client)
    assert len(client.requests) == 1


def test_a_colliding_identifier_is_refused_before_the_second_call_is_paid_for():
    """Two agents that slug alike refuse the paper, and the refusal is free.

    The id is built from the first call's fields alone, so a paper that cannot
    be named is known to be unusable before the second call is made.
    """
    client = StubClient(
        model_response(
            identity_payload(
                experiment_payload(agent="NAD+"),
                experiment_payload(agent="NAD"),
            )
        )
    )

    with pytest.raises(ExperimentIdCollisionError):
        extract_record(paper(), TEXT, client=client)
    assert len(client.requests) == 1


@pytest.mark.parametrize("missing", ["doi", "year"])
def test_missing_paper_identity_raises_rather_than_being_invented(missing):
    """And before either call, because it is knowable from the ingest row alone.

    The record's identity comes from `paper`, not from the prose, so a row that
    cannot supply it can never produce a record however good the extraction is.
    Checking it first makes that free instead of two calls to the expensive
    model.
    """
    client = StubClient(*extraction_responses())
    with pytest.raises(ExtractError, match=f"paper.{missing} is missing"):
        extract_record(paper(**{missing: None}), TEXT, client=client)
    assert client.requests == []


def test_a_paper_with_no_first_author_cannot_be_named_and_says_so():
    client = StubClient(*extraction_responses())
    with pytest.raises(ExtractError, match="experiment_id convention"):
        extract_record(paper(first_author=None), TEXT, client=client)


def test_an_experiment_with_no_agent_value_raises():
    nameless = experiment_payload()
    nameless["intervention"]["agent"] = claim(None)
    client = StubClient(*extraction_responses(nameless))

    with pytest.raises(ModelResponseError, match="intervention.agent.value"):
        extract_record(paper(), TEXT, client=client)


def test_an_other_organism_without_a_species_still_gets_an_identifier():
    unnamed = experiment_payload(organism="other", species=None)
    client = StubClient(*extraction_responses(unnamed))

    record = extract_record(paper(), TEXT, client=client)
    VALIDATOR.validate(record)
    assert record["experiments"][0]["experiment_id"] == "harrison2009-other-rapamycin"
