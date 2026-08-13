"""`python -m extract`: what it processes, what it skips, and how it fails.

The database is the in-memory one from `conftest.engine`, so the selection and
idempotence logic runs against the production table definition; the model client
is a stub, so no test needs a key or a network.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import anthropic
import httpx
import pytest
from jsonschema import Draft202012Validator

from extract.cli import (
    DEFAULT_LIMIT,
    DEFAULT_OUT_ROOT,
    GOLD_ROOT,
    REPO_ROOT,
    RunSummary,
    _papers,
    _write_record,
    build_parser,
    main,
    record_path,
    resolve_out_root,
    run_extraction,
)
from extract.errors import OutputPathError, RecordValidationError
from extract.model import API_KEY_ENV
from extract.schema import SCHEMA_PATH
from ingest.db import store_papers
from ingest.models import RawPaper
from tests.conftest import StubClient, claim, claims, experiment_payload, model_response

VALIDATOR = Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))
VERSION = json.loads(SCHEMA_PATH.read_text())["x-schema-version"]

POSITIVE = {
    "reports_lifespan_intervention": True,
    "reason": "Rapamycin was administered and lifespan measured.",
    "confidence": "high",
}
NEGATIVE = {
    "reports_lifespan_intervention": False,
    "reason": "Review article; no experiment of its own.",
    "confidence": "high",
}
RECORD = {"experiments": [experiment_payload()]}

# A lone surrogate: `json.loads` accepts the `\udbff` escape and hands back a
# Python `str`, jsonschema calls it a perfectly good string, and no UTF-8
# encoder will take it. `model_response` serialises with `ensure_ascii=True`,
# so what the stub replays is the plain ASCII escape a model would actually
# emit.
SURROGATE_RECORD = {
    "experiments": [experiment_payload(mechanism=claim("mTOR inhibition \udbff"))]
}


def nan_body() -> str:
    """The response text for a record whose one number is a bare `NaN` token.

    Built by serialising an actual `float('nan')` with `json.dumps`'s default
    `allow_nan=True`, so the token in the payload is the one Python itself
    writes rather than a string a test author typed.
    """
    payload = experiment_payload()
    payload["lifespan_effect"]["median_change_pct"] = claim(float("nan"))
    return json.dumps({"experiments": [payload]})


def overloaded(status: int = 529) -> anthropic.APIStatusError:
    """The SDK exception an overloaded API raises, as the stub client replays it."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIStatusError(
        "Overloaded", response=httpx.Response(status, request=request), body=None
    )


def paper(pmid: str = "19587680", **overrides: Any) -> RawPaper:
    fields: dict[str, Any] = {
        "source": "pubmed",
        "source_id": pmid,
        "pmid": pmid,
        "doi": f"10.1038/nature{pmid}",
        "title": "Rapamycin fed late in life extends lifespan",
        "abstract": (
            "Rapamycin extended median survival by 14% in genetically "
            "heterogeneous mice."
        ),
        "year": 2009,
        "first_author": "Harrison",
    }
    fields.update(overrides)
    return RawPaper.build(**fields)


def test_a_passing_paper_is_extracted_and_written(engine, tmp_path):
    store_papers(engine, [paper()])
    client = StubClient(model_response(POSITIVE), model_response(RECORD))

    summary = run_extraction(engine, client=client, limit=20, out_root=tmp_path)

    assert (summary.considered, summary.extracted, summary.failed) == (1, 1, 0)
    written = list((tmp_path / VERSION).glob("*.json"))
    assert len(written) == 1
    VALIDATOR.validate(json.loads(written[0].read_text()))


def test_a_written_record_says_it_came_from_the_abstract_it_was_shown(engine, tmp_path):
    """Provenance has to be true of the file on disk, not of a return value.

    `extracted_from` is one third of the provenance triple PLAN.md calls a key
    invariant, and Phase 3 reads it per file to score like against like: a
    record labelled `full_text` that was extracted from an abstract is compared
    against the wrong gold expectation, and nothing downstream can tell.

    Two things have to hold for the label to be honest, and until this test
    neither was pinned anywhere the CLI could break it. The value: every claim
    in the written file says `abstract`, which is what this command extracts
    from. And the text: the abstract is what both stages were actually shown —
    the stored abstract as the text to judge, the title as the title — so the
    label describes the run rather than merely being spelled correctly.
    """
    stored = paper()
    store_papers(engine, [stored])
    client = StubClient(model_response(POSITIVE), model_response(RECORD))

    run_extraction(engine, client=client, limit=20, out_root=tmp_path)

    written = json.loads(record_path(tmp_path / VERSION, stored).read_text())
    assert {c["extracted_from"] for c in claims(written)} == {"abstract"}

    # Exactly two calls, and both were shown the same thing: `Title:` carries
    # the title and the body is the abstract. Swap the two and every quote in
    # the record is checked against the wrong text, while the file still claims
    # abstract provenance.
    shown = f"Title: {stored.title}\n\n{stored.abstract}"
    gate, extraction = client.requests
    assert gate["messages"] == [{"role": "user", "content": shown}]
    assert extraction["messages"] == [{"role": "user", "content": shown}]


def test_a_screened_out_paper_never_reaches_the_expensive_model(engine, tmp_path, capsys):
    store_papers(engine, [paper()])
    client = StubClient(model_response(NEGATIVE))  # a second call would fail the test

    summary = run_extraction(engine, client=client, limit=20, out_root=tmp_path)

    assert (summary.screened_out, summary.extracted) == (1, 0)
    assert len(client.requests) == 1
    assert list((tmp_path / VERSION).glob("*.json")) == []
    assert "Review article" in capsys.readouterr().out


def test_a_second_run_re_extracts_nothing(engine, tmp_path):
    store_papers(engine, [paper()])
    run_extraction(
        engine,
        client=StubClient(model_response(POSITIVE), model_response(RECORD)),
        limit=20,
        out_root=tmp_path,
    )

    quiet = StubClient()  # any model call at all fails the test
    summary = run_extraction(engine, client=quiet, limit=20, out_root=tmp_path)

    assert (summary.considered, summary.already_extracted) == (0, 1)
    assert quiet.requests == []


def test_a_new_schema_version_is_a_different_output_directory(engine, tmp_path):
    """Idempotence is per (paper, schema_version), so a bump re-extracts."""
    store_papers(engine, [paper()])
    stale = tmp_path / "0.0.1"
    stale.mkdir()
    # The same paper, already extracted against an older schema version.
    record_path(stale, paper()).write_text("{}")

    summary = run_extraction(
        engine,
        client=StubClient(model_response(POSITIVE), model_response(RECORD)),
        limit=20,
        out_root=tmp_path,
    )
    assert summary.extracted == 1


def test_a_failure_is_loud_counted_and_does_not_strand_the_other_papers(
    engine, tmp_path, capsys
):
    store_papers(engine, [paper("19587680"), paper("19801973")])
    client = StubClient(
        model_response(POSITIVE),
        model_response("not JSON"),  # first paper: unparseable
        model_response("still not JSON"),  # ... and its one retry
        model_response(POSITIVE),
        model_response(RECORD),  # second paper: fine
    )

    summary = run_extraction(engine, client=client, limit=20, out_root=tmp_path)

    assert (summary.considered, summary.extracted, summary.failed) == (2, 1, 1)
    captured = capsys.readouterr()
    assert "FAIL" in captured.err
    assert "not JSON" in captured.err
    assert len(list((tmp_path / VERSION).glob("*.json"))) == 1


def test_an_api_error_on_one_paper_does_not_strand_the_next(engine, tmp_path, capsys):
    """A 529 is a per-paper failure, exactly like an unparseable payload.

    Before the wrap at the model boundary, the SDK exception was none of the
    types this loop catches, so the first overloaded response ended the run and
    the remaining papers were never seen.
    """
    store_papers(engine, [paper("19587680"), paper("19801973")])
    client = StubClient(
        overloaded(),  # first paper: the API is overloaded at the gate
        model_response(POSITIVE),
        model_response(RECORD),  # second paper: fine
    )

    summary = run_extraction(engine, client=client, limit=20, out_root=tmp_path)

    assert (summary.considered, summary.extracted, summary.failed) == (2, 1, 1)
    captured = capsys.readouterr()
    assert "FAIL" in captured.err
    assert "529" in captured.err
    assert len(list((tmp_path / VERSION).glob("*.json"))) == 1


def test_main_exits_non_zero_and_names_an_api_failure(engine, tmp_path, monkeypatch, capsys):
    """The operator gets the actionable message, not a raw SDK traceback."""
    store_papers(engine, [paper()])
    client = StubClient(overloaded())
    monkeypatch.setattr("extract.cli.make_client", lambda: client)
    monkeypatch.setattr("extract.cli.make_engine", lambda: engine)

    status = main(["--out", str(tmp_path)])

    assert status == 1
    captured = capsys.readouterr().err
    assert "529" in captured
    assert "No record was produced" in captured


def test_a_paper_with_no_usable_abstract_is_skipped_and_reported(engine, tmp_path, capsys):
    """Both kinds of unusable abstract, because both actually arrive from PubMed.

    `_papers` reads "no abstract" as absent *or* blank, and `ingest/models.py`
    stores `abstract` verbatim with no normalisation — so a `<AbstractText>`
    element holding only whitespace reaches the database as a run of spaces,
    not as NULL. A fixture of one `None` leaves the second half of that guard
    untested, and losing it is not a cosmetic regression: the blank paper stops
    being counted here, reaches `_process` where `abstract or ""` keeps the
    spaces, `classify` raises on the empty text, and a data condition PLAN.md
    treats as a warning at exit 0 is counted as a failure that exits 1.
    """
    store_papers(engine, [paper("19587680", abstract=None), paper("19587681", abstract="   ")])
    client = StubClient()

    summary = run_extraction(engine, client=client, limit=20, out_root=tmp_path)

    assert (summary.no_abstract, summary.considered, summary.failed) == (2, 0, 0)
    assert client.requests == []
    assert "no abstract" in capsys.readouterr().out


def test_the_limit_counts_attempts_not_rows(engine, tmp_path):
    """A skipped paper does not consume the limit — either kind of skipped paper.

    The contract is written down twice, in `--help` and on `_papers`, and it is
    only testable when "attempts" and "rows read" are different numbers. So the
    fixture puts one skipped paper of each kind — already extracted, and no
    abstract — ahead of three fresh ones, and `--limit 2` has to reach past
    both. A fixture of three fresh papers cannot fail this test whichever rule
    is implemented, which is what the previous version of it was.

    The rule matters at the far end: under the opposite one, `--limit 20`
    against a database whose first twenty rows are already extracted processes
    nothing and prints "nothing to do" on every run forever.
    """
    out_dir = tmp_path / VERSION
    out_dir.mkdir(parents=True)
    done = paper("19587680")
    silent = paper("19587681", abstract=None)
    fresh = [paper("19587682"), paper("19587683"), paper("19587684")]
    store_papers(engine, [done, silent, *fresh])
    record_path(out_dir, done).write_text("{}")

    ordered = [done, silent, *fresh]
    # This test's own premise, and nothing more: its constants are in ascending
    # dedup_key order, so the two skipped papers are the ones `_papers` reads
    # first. That the query *sorts* is a separate claim about the query, and is
    # pinned separately — see the ordering test below.
    assert [p.dedup_key for p in ordered] == sorted(p.dedup_key for p in ordered)

    # Two attempts' worth of responses: a third attempt exhausts the queue and
    # fails the test rather than quietly succeeding.
    client = StubClient(
        model_response(POSITIVE),
        model_response(RECORD),
        model_response(POSITIVE),
        model_response(RECORD),
    )

    summary = run_extraction(engine, client=client, limit=2, out_root=tmp_path)

    assert summary.considered == 2
    assert (summary.already_extracted, summary.no_abstract) == (1, 1)
    assert summary.extracted == 2
    # The two attempts were the first two *fresh* papers: the skipped rows were
    # read past, and the third fresh paper is what the limit actually stopped.
    assert {path.name for path in out_dir.iterdir()} == {
        record_path(out_dir, done).name,
        record_path(out_dir, fresh[0]).name,
        record_path(out_dir, fresh[1]).name,
    }


def test_papers_are_read_in_dedup_key_order_not_insertion_order(engine, tmp_path):
    """`--limit 20` has to select the same twenty papers on every run.

    Without the `ORDER BY`, which twenty is whatever the database hands back.
    SQLite obliges by returning rowid order, so a fixture inserted in key order
    cannot tell a sorted query from an unsorted one — and PLAN.md's production
    database is PostgreSQL, where row order without `ORDER BY` is unspecified
    and free to change between runs of the same query. So these rows go in in
    an order that is not dedup_key order, and the assertion is that they come
    back in dedup_key order anyway.
    """
    out_dir = tmp_path / VERSION
    out_dir.mkdir(parents=True)
    shuffled = [paper(pmid) for pmid in ("19587684", "19587681", "19587683", "19587680")]
    store_papers(engine, shuffled)

    inserted = [p.dedup_key for p in shuffled]
    # The premise: insertion order and key order are genuinely different, so an
    # unsorted query returns something this assertion can see.
    assert inserted != sorted(inserted)

    summary = RunSummary()
    read = [
        p.dedup_key
        for p in _papers(engine, limit=len(shuffled), out_dir=out_dir, summary=summary)
    ]

    assert read == sorted(inserted)


def test_an_empty_database_says_so(engine, tmp_path, capsys):
    summary = run_extraction(engine, client=StubClient(), limit=20, out_root=tmp_path)

    assert summary.considered == 0
    assert "nothing to do" in capsys.readouterr().out


def test_record_paths_stay_distinct_for_keys_that_slugify_alike(tmp_path):
    """A collision would look exactly like 'already extracted' — the silent failure."""
    first = record_path(tmp_path, paper(doi="10.1000/a-b"))
    second = record_path(tmp_path, paper(doi="10.1000/a.b"))
    assert first != second


def test_the_guarded_directory_is_the_repositorys_own_gold_set():
    """Premise of the refusal tests: they name the directory that actually matters."""
    assert GOLD_ROOT == REPO_ROOT / "data" / "gold"
    assert GOLD_ROOT.is_dir()


@pytest.mark.parametrize("spelling", ["plain", "parent_segment", "case_variant", "symlink"])
def test_every_spelling_of_the_gold_directory_is_refused(spelling, tmp_path):
    """One directory, four ways to name it, and none of them may be written to.

    An extracted record is shape-identical to a gold one and passes the same
    schema, so unreviewed model output landing in `data/gold/` would be
    indistinguishable from ground truth and Phase 3 would score the model
    against its own output. Comparing the strings as given catches only the
    first of these; the paths are resolved and compared component by component,
    case-insensitively.

    Nothing is created inside the gold set by this test — `resolve_out_root`
    refuses before any directory is made — and the symlink lives in `tmp_path`.
    """
    candidate = {
        "plain": GOLD_ROOT,
        "parent_segment": GOLD_ROOT.parent / "extracted" / ".." / "gold",
        "case_variant": GOLD_ROOT.parent / "GOLD",
        "symlink": tmp_path / "records",
    }[spelling]
    if spelling == "symlink":
        candidate.symlink_to(GOLD_ROOT, target_is_directory=True)

    with pytest.raises(OutputPathError, match="gold set"):
        resolve_out_root(candidate)


def test_an_ordinary_out_path_is_accepted_and_returned_resolved(tmp_path):
    resolved = resolve_out_root(tmp_path / "records" / ".." / "records")
    assert resolved == (tmp_path / "records").resolve()


def test_a_sibling_whose_name_merely_starts_with_gold_is_allowed():
    """The comparison is per path component, not a string prefix."""
    assert resolve_out_root(GOLD_ROOT.parent / "goldilocks")


def test_a_run_into_the_gold_set_writes_nothing_and_calls_no_model(
    engine, tmp_path, monkeypatch
):
    """The refusal is at the chokepoint, before the directory and before the spend.

    `GOLD_ROOT` is redirected at a stand-in so that this test cannot touch the
    real gold set even if the guard it is testing is removed;
    `test_the_guarded_directory_is_the_repositorys_own_gold_set` pins the
    constant to the real one.
    """
    gold = tmp_path / "data" / "gold"
    gold.mkdir(parents=True)
    monkeypatch.setattr("extract.cli.GOLD_ROOT", gold)
    store_papers(engine, [paper()])
    client = StubClient(model_response(POSITIVE), model_response(RECORD))

    with pytest.raises(OutputPathError, match="gold set"):
        run_extraction(engine, client=client, limit=20, out_root=gold / "records")

    assert list(gold.rglob("*")) == []
    assert client.requests == []


def test_main_reports_a_gold_out_path_as_an_invalid_argument(
    engine, tmp_path, monkeypatch, capsys
):
    gold = tmp_path / "data" / "gold"
    gold.mkdir(parents=True)
    monkeypatch.setattr("extract.cli.GOLD_ROOT", gold)
    monkeypatch.setattr("extract.cli.make_client", lambda: StubClient())
    monkeypatch.setattr("extract.cli.make_engine", lambda: engine)

    status = main(["--out", str(gold)])

    assert status == 2
    assert str(gold) in capsys.readouterr().err


def test_an_interrupted_write_leaves_nothing_a_later_run_would_skip(
    engine, tmp_path, monkeypatch
):
    """A record file's existence is the only "already extracted" marker there is.

    `write_text` truncates the destination as it opens it, so a Ctrl-C between
    that and the last byte left a truncated .json that every later run skipped
    and Phase 3 would have read as a record. The bytes go to a temporary file in
    the destination's own directory — same filesystem, so the rename is atomic —
    and an interruption before the rename leaves neither a destination file nor
    the temporary one.
    """
    store_papers(engine, [paper()])
    renamed: list[Path] = []

    def interrupt(src: Any, dst: Any) -> None:
        renamed.append(Path(src))
        raise KeyboardInterrupt

    monkeypatch.setattr("extract.cli.os.replace", interrupt)
    client = StubClient(model_response(POSITIVE), model_response(RECORD))

    with pytest.raises(KeyboardInterrupt):
        run_extraction(engine, client=client, limit=20, out_root=tmp_path)

    out_dir = tmp_path / VERSION
    assert list(out_dir.iterdir()) == []
    assert renamed and renamed[0].parent == out_dir


def test_an_unwritable_record_is_counted_and_does_not_strand_the_next_paper(
    engine, tmp_path, monkeypatch, capsys
):
    """The write is the last step of a paper whose model calls are already paid for.

    An `OSError` there used to escape both the per-paper handler and `main`: the
    remaining papers were never attempted and the summary was never printed.
    """
    store_papers(engine, [paper("19587680"), paper("19801973")])
    monkeypatch.setattr("extract.cli.make_engine", lambda: engine)
    monkeypatch.setattr(
        "extract.cli.make_client",
        lambda: StubClient(
            model_response(POSITIVE),
            model_response(RECORD),  # first paper: extracted, then unwritable
            model_response(POSITIVE),
            model_response(RECORD),  # second paper: fine
        ),
    )
    replace = os.replace
    failed: list[Any] = []

    def fail_once(src: Any, dst: Any) -> None:
        if not failed:
            failed.append(dst)
            raise OSError(28, "No space left on device")
        replace(src, dst)

    monkeypatch.setattr("extract.cli.os.replace", fail_once)

    status = main(["--out", str(tmp_path)])

    assert status == 1
    captured = capsys.readouterr()
    assert "No space left on device" in captured.err
    assert "2 considered, 1 extracted" in captured.out
    assert "1 failed" in captured.out
    # One record, and no temporary file left over from the failed write.
    assert len(list((tmp_path / VERSION).iterdir())) == 1


# --- a record that cannot become JSON at all ---
#
# Two shapes, both arriving as model-derived text and both raising a plain
# `ValueError` at the write. `main` used to catch `ValueError` to recognise a
# bad `--out`, so the first of these was reported as an invalid argument and
# exited 2, with no FAIL line, no summary, and the rest of the batch never
# attempted. They are content failures: per-paper, counted, exit 1.


def test_an_unencodable_record_fails_one_paper_and_not_the_batch(
    engine, tmp_path, monkeypatch, capsys
):
    """A lone surrogate is neither `ExtractError` nor `OSError`; it is still per-paper."""
    store_papers(engine, [paper("19587680"), paper("19801973")])
    monkeypatch.setattr("extract.cli.make_engine", lambda: engine)
    monkeypatch.setattr(
        "extract.cli.make_client",
        lambda: StubClient(
            model_response(POSITIVE),
            model_response(SURROGATE_RECORD),  # first paper: unencodable
            model_response(POSITIVE),
            model_response(RECORD),  # second paper: fine
        ),
    )

    status = main(["--out", str(tmp_path)])

    # 1, not 2: this is the data being wrong, not the command line.
    assert status == 1
    captured = capsys.readouterr()
    assert "FAIL" in captured.err
    assert paper("19587680").dedup_key in captured.err
    assert "invalid argument" not in captured.err
    # The run reached the end and said what it did.
    assert "2 considered, 1 extracted" in captured.out
    assert "1 failed" in captured.out
    # Only the second paper's record, and no leftover temporary file.
    written = list((tmp_path / VERSION).iterdir())
    assert [p.name for p in written] == [record_path(tmp_path / VERSION, paper("19801973")).name]


def test_a_bare_nan_never_becomes_a_record_and_leaves_the_paper_pending(
    engine, tmp_path, capsys
):
    """The half that matters: a refused record must not mark the paper extracted.

    A record file's existence is the only idempotence marker there is, so a
    `NaN` that reached disk would be permanent — every later run would skip the
    paper, and Phase 4 would read a token `JSON.parse` throws on.
    """
    store_papers(engine, [paper()])
    client = StubClient(
        model_response(POSITIVE),
        model_response(nan_body()),
        model_response(nan_body()),  # ... and its one retry
    )

    summary = run_extraction(engine, client=client, limit=20, out_root=tmp_path)

    assert (summary.considered, summary.extracted, summary.failed) == (1, 0, 1)
    # Refused where an untrusted payload is refused — at the parse, naming the
    # token and carrying a window of what arrived — rather than surviving as a
    # float as far as the write.
    failure = capsys.readouterr().err
    assert "NaN" in failure and "payload excerpt" in failure
    assert list((tmp_path / VERSION).iterdir()) == []

    # Still pending: a later run attempts it again rather than counting it done.
    again = StubClient(model_response(POSITIVE), model_response(RECORD))
    second = run_extraction(engine, client=again, limit=20, out_root=tmp_path)

    assert (second.considered, second.already_extracted, second.extracted) == (1, 0, 1)


def test_the_write_refuses_a_non_finite_number_and_writes_nothing(tmp_path):
    """The second lock, reachable only from here now that parsing holds the first.

    `json.dumps` defaults to `allow_nan=True` and would write the bare token.
    Tested directly because `extract/model.py` refuses such a payload on the
    way in, so no model response can reach this line — which is exactly why it
    would rot unnoticed without a test of its own.
    """
    with pytest.raises(RecordValidationError, match="cannot be serialised"):
        _write_record(tmp_path / "record.json", {"median_change_pct": float("nan")})

    assert list(tmp_path.iterdir()) == []


def test_the_write_refuses_unencodable_text_and_writes_nothing(tmp_path):
    """Neither the destination nor the temporary file may survive the refusal."""
    with pytest.raises(RecordValidationError, match="UTF-8"):
        _write_record(tmp_path / "record.json", {"mechanism": "mTOR \udbff"})

    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root writes to a read-only directory regardless of its mode",
)
def test_a_read_only_out_directory_aborts_before_any_model_call(
    engine, tmp_path, monkeypatch, capsys
):
    """The probe's whole point: the run must cost nothing when it cannot store anything.

    `mkdir(exist_ok=True)` succeeds on a read-only directory, so without the
    probe each of these two papers would be classified and extracted — two
    model calls apiece — before failing at the write, and the per-paper
    `OSError` handler would let the next paper repeat it.
    """
    store_papers(engine, [paper("19587680"), paper("19801973")])
    out_dir = tmp_path / VERSION
    out_dir.mkdir(parents=True)
    client = StubClient()  # queues nothing: any model call is an error
    monkeypatch.setattr("extract.cli.make_engine", lambda: engine)
    monkeypatch.setattr("extract.cli.make_client", lambda: client)

    out_dir.chmod(0o500)
    try:
        status = main(["--out", str(tmp_path)])
    finally:
        # Restored here rather than left to tmp_path cleanup, so a failing
        # assertion below cannot leave an undeletable directory behind.
        out_dir.chmod(0o700)

    assert status == 1
    assert client.requests == []
    captured = capsys.readouterr()
    assert str(out_dir) in captured.err
    assert "considered" not in captured.out
    assert list(out_dir.iterdir()) == []


def test_an_out_path_that_is_a_file_is_reported_rather_than_raised(
    engine, tmp_path, monkeypatch, capsys
):
    """An ordinary operator typo: `mkdir` raises OSError before any paper is read."""
    blocker = tmp_path / "records"
    blocker.write_text("not a directory\n")
    monkeypatch.setattr("extract.cli.make_client", lambda: StubClient())
    monkeypatch.setattr("extract.cli.make_engine", lambda: engine)

    status = main(["--out", str(blocker)])

    assert status == 1
    assert str(blocker) in capsys.readouterr().err


def test_a_missing_api_key_aborts_the_run(monkeypatch, capsys):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    assert main(["--limit", "1"]) == 1
    assert API_KEY_ENV in capsys.readouterr().err


def test_the_limit_must_be_a_positive_integer():
    parser = build_parser()
    for bad in (["--limit", "0"], ["--limit", "not-a-number"]):
        with pytest.raises(SystemExit):
            parser.parse_args(bad)


def test_the_defaults_are_the_ones_the_help_text_advertises():
    """`--help` is documentation the code emits, and it can be wrong like any other.

    Both defaults are interpolated into their help strings from the same
    constants the parser is configured with — but only by convention, and a
    changed default with an unchanged constant would leave `--help` quietly
    lying about what the command does. Asserting the parsed value and the
    printed sentence together is what keeps the two honest.
    """
    parser = build_parser()
    parsed = parser.parse_args([])
    assert parsed.limit == DEFAULT_LIMIT
    assert parsed.out == DEFAULT_OUT_ROOT

    # Every whitespace character is removed rather than collapsed: argparse
    # wraps help text to the terminal width, and a path longer than that width
    # is broken mid-word with no space to collapse. Stripping all whitespace
    # makes the assertion independent of the terminal and of where the
    # repository happens to live.
    printed = "".join(parser.format_help().split())
    assert f"(default:{DEFAULT_LIMIT})" in printed
    assert f"(default:{DEFAULT_OUT_ROOT})" in printed


def test_the_module_entry_point_exits_with_mains_status(monkeypatch):
    """`python -m extract` must carry a failed run's status out to the shell.

    `extract/__main__.py` is a two-line shim, and the line that matters is
    `sys.exit(main())` rather than a bare `main()`. Without the `sys.exit` the
    module still runs the batch, still prints every `FAIL` line, and still
    prints the summary — and then exits 0, because a return value from a module
    body goes nowhere. A caller that reports success on a run with failures is
    the silent fallback CLAUDE.md forbids and the opposite of the loud failure
    PLAN.md asks for, and nothing else in this suite executes the shim: every
    other test calls `main` directly, which is exactly the reach the shim
    exists to be outside of.

    `runpy` is what makes the assertion possible without a database, a key, or
    a subprocess: `main` is stubbed, so what is under test is only whether its
    status reaches the interpreter.
    """
    import runpy

    for status in (1, 2, 0):
        calls: list[bool] = []

        def fake_main(argv=None, _status=status, _calls=calls):
            _calls.append(True)
            return _status

        monkeypatch.setattr("extract.cli.main", fake_main)
        with pytest.raises(SystemExit) as raised:
            runpy.run_module("extract", run_name="__main__")
        assert calls == [True], "the shim did not call main"
        assert raised.value.code == status
