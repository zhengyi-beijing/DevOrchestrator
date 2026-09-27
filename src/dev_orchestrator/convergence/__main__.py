"""Package entry point for python -m dev_orchestrator.convergence."""
from __future__ import annotations

import sys
from dev_orchestrator.convergence.cli import main

if __name__ == "__main__":
    sys.exit(main())
