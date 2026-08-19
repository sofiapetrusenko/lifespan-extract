"""Entry point for `python -m extract`.

A shim only: `main` lives in `extract.cli` so tests can import it without
importing a module named `__main__`.
"""

import sys

from extract.cli import main

if __name__ == "__main__":
    sys.exit(main())
