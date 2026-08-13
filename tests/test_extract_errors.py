"""The package's error surface: everything catchable is reachable.

`extract/__init__.py` exists to let a caller catch this package's failures
without importing the Anthropic SDK. That promise is only kept if every public
error type is actually re-exported, and twice now one was not — `ModelCallError`,
the type `extract/cli.py`'s own docstring names as what makes transport failures
catchable, and `ExperimentIdCollisionError`, added a round later.

The expectation is derived from `extract.errors` itself rather than written out
here. A hand-written list would be the same drift one level up: adding a type
and forgetting the list would leave this file green.
"""

from __future__ import annotations

import extract
from extract import errors


def public_error_types() -> set[str]:
    """Return the names of every public exception class defined in `extract.errors`.

    Keyed on `__module__` so a type merely imported into `extract.errors` — the
    `ingest.errors` helpers it pulls in — is not counted as one of its own.
    """
    return {
        name
        for name, value in vars(errors).items()
        if not name.startswith("_")
        and isinstance(value, type)
        and issubclass(value, BaseException)
        and value.__module__ == errors.__name__
    }


def test_the_error_module_declares_every_type_it_defines():
    """Premise of the test below: `__all__` is not itself the thing that drifted."""
    assert public_error_types() == set(errors.__all__)


def test_every_public_error_type_is_re_exported_from_the_package():
    """The same class object, not merely a name that happens to resolve."""
    expected = public_error_types()
    assert expected, "no exception types found; the derivation above is broken"

    exported = {name: getattr(extract, name, None) for name in expected}
    missing = sorted(name for name, value in exported.items() if value is None)
    assert not missing, f"{missing} are public in extract.errors but not in extract"
    assert all(value is getattr(errors, name) for name, value in exported.items())


def test_the_packages_all_is_exactly_that_set():
    """`from extract import *` reaches every error type and nothing invented."""
    assert set(extract.__all__) == public_error_types()
