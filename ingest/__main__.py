"""Entry point for `python -m ingest`.

A shim only: `main` lives in `ingest.cli` so tests can import it without
importing a module named `__main__`.
"""

import sys

from ingest.cli import main

if __name__ == "__main__":
    sys.exit(main())
