"""PubDate year extraction, against verbatim PubMed responses.

**Every file under `tests/fixtures/pubmed_pubdate/` is an unedited efetch
response**, saved on 2026-08-17 straight from
`eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&retmode=xml&id=…`.
Nothing was reformatted, trimmed or hand-written. That is the point of this
module: the defect it pins was invisible to every existing ingest fixture
because all of them happened to carry a PubDate shape that parsed, and a
hand-written fixture would have been written from the same assumption that
produced the bug.

One fixture per distinct PubDate shape observed in the 30 rows a live run
ingested:

* `efetch_40773213.xml` — `Year` + `Month` + `Day`, flattening to `2025Sep23`
* `efetch_40609839.xml` — `Year` + `Month`, flattening to `2025Aug`
* `efetch_42207784.xml` — `Year` alone, `2026`

The first two returned None before the fix; the third is the shape that made 2
of 30 rows look fine while 28 came back `year IS NULL`.

`MedlineDate` is exercised from an inline document rather than a fixture. It is
covered here because `_year` has a branch for it, but no MedlineDate PubDate
appeared in any live batch probed while writing these tests (≈90 records across
five queries, including a 1985–1995 gerontology-journal range where seasonal
dates would be likeliest). The inline document below is therefore hand-written
and labelled as such, so the verbatim claim above stays true of every file.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from ingest.pubmed import _year

FIXTURES = Path(__file__).parent / "fixtures" / "pubmed_pubdate"


def article(pmid: str) -> ET.Element:
    """Return the `Article` element of a saved efetch response."""
    root = ET.parse(FIXTURES / f"efetch_{pmid}.xml").getroot()
    found = root.find("PubmedArticle/MedlineCitation/Article")
    assert found is not None, f"fixture efetch_{pmid}.xml has no Article element"
    return found


def inline(pubdate: str) -> ET.Element:
    """Build an Article around a PubDate fragment. Hand-written, not a fixture."""
    return ET.fromstring(
        "<Article><Journal><JournalIssue>"
        f"{pubdate}"
        "</JournalIssue></Journal></Article>"
    )


# --------------------------------------------------------------------------
# the shapes the live system actually produced
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pmid", "expected"),
    [
        ("40773213", 2025),  # Year + Month + Day
        ("40609839", 2025),  # Year + Month
        ("42207784", 2026),  # Year alone
    ],
)
def test_every_live_pubdate_shape_yields_its_year(pmid: str, expected: int) -> None:
    """The regression, pinned on the responses that exposed it.

    Two of these three came back None before the fix, and the third did not —
    which is why the defect reached 28 rows without a single test failing.
    """
    assert _year(article(pmid)) == expected


@pytest.mark.parametrize("pmid", ["40773213", "40609839"])
def test_the_flattened_pubdate_is_still_unparseable_by_search(pmid: str) -> None:
    """The mechanism, not just the outcome.

    Asserts the *cause*: flattening these PubDates runs the year into the month
    with no separator. A future refactor that goes back to searching flattened
    text would pass the test above only if it also fixed this, and this test
    says which of the two the fix relies on.
    """
    pubdate = article(pmid).find("Journal/JournalIssue/PubDate")
    flattened = "".join(pubdate.itertext()).strip()

    assert flattened[:4].isdigit() and flattened[4:5].isalpha(), (
        f"{flattened!r} no longer runs the year into a letter; this test's "
        "premise is gone and the fix may no longer be load-bearing"
    )


# --------------------------------------------------------------------------
# MedlineDate free text — hand-written, see the module docstring
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2025 Nov-Dec", 2025),
        ("1998 Winter", 1998),
        ("2025Nov-Dec", 2025),  # no separator: the digit-boundary case
        ("1998 Nov-1999 Jan", 1998),  # first year wins, not the last
    ],
)
def test_a_medline_date_still_parses(text: str, expected: int) -> None:
    """MedlineDate is genuinely free text and is the one place to search it."""
    assert _year(inline(f"<PubDate><MedlineDate>{text}</MedlineDate></PubDate>")) == expected


# --------------------------------------------------------------------------
# the rule that survives: a year is never inferred
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pubdate",
    [
        "",  # no PubDate element at all
        "<PubDate></PubDate>",  # present but empty
        "<PubDate><Month>Sep</Month><Day>23</Day></PubDate>",  # month and day only
        "<PubDate><MedlineDate>n.d.</MedlineDate></PubDate>",  # no digits
        "<PubDate><MedlineDate>Spring</MedlineDate></PubDate>",
        "<PubDate><Year>25</Year></PubDate>",  # two digits is not a year
        "<PubDate><MedlineDate>20255</MedlineDate></PubDate>",  # five digits
        "<PubDate><MedlineDate>12025</MedlineDate></PubDate>",
        "<PubDate><MedlineDate>1700</MedlineDate></PubDate>",  # outside the range
    ],
)
def test_no_four_digit_year_yields_none(pubdate: str) -> None:
    """Absent stays absent. The extractor refuses to guess, and must keep doing so.

    `2100` and `1700` are outside the pattern deliberately: a year is read, not
    reconstructed, and a bare number that cannot be a publication year is not
    evidence of one.
    """
    assert _year(inline(pubdate)) is None


def test_a_year_child_is_preferred_over_medline_date() -> None:
    """Both present is malformed per the DTD; the named element still wins."""
    both = "<PubDate><Year>2019</Year><MedlineDate>2001 Fall</MedlineDate></PubDate>"
    assert _year(inline(both)) == 2019
