"""Allow ``python -m paper_notes``."""

import sys

from paper_notes.cli import main

if __name__ == "__main__":
    sys.exit(main())
