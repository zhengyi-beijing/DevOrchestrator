"""Provider-neutral agent backend + router foundation.

This slice is infrastructure only: it defines the frozen execution domain
model, the abstract backend contract, an explicit registry, a deterministic
fail-closed router, and one real ``dsh`` backend. Nothing here starts a model
during probe/routing and no workflow (PHASE_AUTO) consumes this layer yet.
"""

from __future__ import annotations
