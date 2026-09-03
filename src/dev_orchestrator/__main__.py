"""Module entry point: ``python -m dev_orchestrator <command>``."""

import sys

from dev_orchestrator.cli import main

if __name__ == "__main__":
    sys.exit(main())
