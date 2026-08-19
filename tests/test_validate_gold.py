"""Tests for scripts/validate_gold.py's gold-set floor.

The per-file checks are covered by the committed gold set itself, which CI
validates on every push. What is *not* self-covering is the count: every other
check in that script is per-file, so a deleted file is the one corruption none
of them can see. This file pins the floor that closes it.

`GOLD_DIR` is monkeypatched to a temp directory throughout. Nothing here reads
or writes the real `data/gold/` — it is human-controlled ground truth, and a
test that pointed at it could only be made to pass by changing it.
"""

from __future__ import annotations

import json
import shutil

import pytest
import validate_gold as vg
from validate_gold import EXPECTED_GOLD_FILES, GOLD_DIR, MANIFEST_NAME, main


def real_gold_files() -> list:
    """The committed gold files, read-only, as the source for temp copies."""
    return sorted(GOLD_DIR.glob("*.json"))


def test_the_floor_matches_the_committed_set() -> None:
    """The constant is a floor under reality, not a number typed once.

    If a labeller adds an eleventh paper this stays green — `>=` is the point.
    It fails if the constant is ever raised past what is actually committed,
    which would make CI red for everyone with no gold change to explain it.
    """
    assert EXPECTED_GOLD_FILES <= len(real_gold_files()), (
        f"EXPECTED_GOLD_FILES is {EXPECTED_GOLD_FILES} but only "
        f"{len(real_gold_files())} file(s) are committed"
    )


def test_one_file_short_of_the_floor_fails(tmp_path, monkeypatch, capsys) -> None:
    """One deletion is the case the old branch could not see.

    The empty directory was at least stated out loud ("Nothing to validate")
    even while it returned 0. Nine of ten said nothing at all: every file
    present validated, so the run was clean by every check the script had.
    """
    thinned = tmp_path / "gold"
    thinned.mkdir()
    keep = real_gold_files()[: EXPECTED_GOLD_FILES - 1]
    for path in keep:
        shutil.copy(path, thinned / path.name)
    monkeypatch.setattr(vg, "GOLD_DIR", thinned)
    # The script reports paths relative to the repo root; move that too, or
    # a temp directory outside the tree cannot be described.
    monkeypatch.setattr(vg, "REPO_ROOT", tmp_path)

    assert main([]) == 1

    err = capsys.readouterr().err
    assert str(len(keep)) in err and str(EXPECTED_GOLD_FILES) in err, (
        f"the message names neither the count found nor the floor: {err!r}"
    )


def test_an_empty_gold_directory_fails(tmp_path, monkeypatch) -> None:
    """The case that used to return 0, so `rm data/gold/*.json` left CI green."""
    empty = tmp_path / "gold"
    empty.mkdir()
    monkeypatch.setattr(vg, "GOLD_DIR", empty)
    monkeypatch.setattr(vg, "REPO_ROOT", tmp_path)

    assert main([]) == 1


def test_the_floor_alone_does_not_pass_a_thinned_set(tmp_path, monkeypatch) -> None:
    """Meeting the count is necessary, not sufficient — the files still validate.

    Guards against a floor that short-circuits: enough files present, one of
    them malformed, and the per-file checks must still run and still fail.
    """
    padded = tmp_path / "gold"
    padded.mkdir()
    for path in real_gold_files()[:EXPECTED_GOLD_FILES]:
        shutil.copy(path, padded / path.name)
    broken = padded / min(p.name for p in padded.glob("*.json"))
    broken.write_text(json.dumps({"schema_version": "0.4.0"}))
    monkeypatch.setattr(vg, "GOLD_DIR", padded)
    monkeypatch.setattr(vg, "REPO_ROOT", tmp_path)

    assert main([]) == 1


def test_the_committed_set_passes() -> None:
    """The floor must not have broken the run it guards.

    Reads the real gold set — the only test here that does — because the thing
    worth asserting is that today's committed set validates. It writes nothing.

    Skips until `MANIFEST.sha256` exists, because generating it writes into
    `data/gold/` and is the human's to run. The skip is the precondition stated
    out loud, not a failure hidden: the moment the manifest is generated this
    becomes an assertion again, and it is the only test that reads the real set.
    """
    if not (GOLD_DIR / MANIFEST_NAME).is_file():
        pytest.skip(
            f"{MANIFEST_NAME} not generated yet — run "
            "`python scripts/validate_gold.py --write-manifest`"
        )
    assert main([]) == 0
