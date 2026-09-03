"""DevOrchestrator Core control plane.

Pure policy for the Web Sol event/protocol contract: event eligibility, the
request/response protocol models, the fail-closed decision guard, and the
strict fresh repository-truth reader the guard consumes. No transport, no
network/send code, no Worker execution and no automatic task/stage transition
live here — this slice is policy only.
"""

from __future__ import annotations
