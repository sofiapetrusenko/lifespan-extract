"""`python -m extract`: what it processes, what it skips, and how it fails.

The database is the in-memory one from `conftest.engine`, so the selection and
idempotence logic runs against the production table definition; the model client
is a stub, so no test needs a key or a network.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx
import pytest
from jsonschema import Draft202012Validator

from extract.cli import build_parser, main, record_path, run_extraction
from extract.model import API_KEY_ENV
from extract.schema import SCHEMA_PATH
from ingest.db import store_papers
from ingest.models import RawPaper
from tests.conftest import StubClient, experiment_payload, model_response

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


def test_a_paper_without_an_abstract_is_skipped_and_reported(engine, tmp_path, capsys):
    store_papers(engine, [paper(abstract=None)])
    client = StubClient()

    summary = run_extraction(engine, client=client, limit=20, out_root=tmp_path)

    assert (summary.no_abstract, summary.considered) == (1, 0)
    assert client.requests == []
    assert "no abstract" in capsys.readouterr().out


def test_the_limit_counts_attempts_not_rows(engine, tmp_path):
    store_papers(engine, [paper(str(19587680 + n)) for n in range(3)])
    client = StubClient(model_response(NEGATIVE), model_response(NEGATIVE))

    summary = run_extraction(engine, client=client, limit=2, out_root=tmp_path)

    assert summary.considered == 2


def test_an_empty_database_says_so(engine, tmp_path, capsys):
    summary = run_extraction(engine, client=StubClient(), limit=20, out_root=tmp_path)

    assert summary.considered == 0
    assert "nothing to do" in capsys.readouterr().out


def test_record_paths_stay_distinct_for_keys_that_slugify_alike(tmp_path):
    """A collision would look exactly like 'already extracted' — the silent failure."""
    first = record_path(tmp_path, paper(doi="10.1000/a-b"))
    second = record_path(tmp_path, paper(doi="10.1000/a.b"))
    assert first != second


def test_a_missing_api_key_aborts_the_run(monkeypatch, capsys):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    assert main(["--limit", "1"]) == 1
    assert API_KEY_ENV in capsys.readouterr().err


def test_the_limit_must_be_a_positive_integer():
    parser = build_parser()
    for bad in (["--limit", "0"], ["--limit", "not-a-number"]):
        with pytest.raises(SystemExit):
            parser.parse_args(bad)
